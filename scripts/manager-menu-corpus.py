#!/usr/bin/env python3
"""Measure the manager-menu guard against a corpus drawn from real manager transcripts.

The guard (`scripts/manager-menu-guard.py`) denies a manager session's `AskUserQuestion`
when an option offers a manager-owned action as a menu choice. Its predicate is only as
good as the population it is measured against, and a hand-written list of cases measures
the author's imagination rather than the fleet. This scanner builds the corpus the guard
is actually graded on: every `AskUserQuestion` a **manager** session raised, with its
option labels and timestamp, read from the transcripts themselves.

⚠️ **This is a REPLAY, not a log read.** The guard's own record
(`~/.claude/state/manager-menu-guard/<session_id>.jsonl`) says what the guard *decided*;
this scanner recomputes the predicate from the labels in the transcript. That distinction
is the whole point — grading a gate on its own output would pass any gate that decided
consistently, including one that decided wrongly every time.

⚠️ **Deduplicate by project dir, or the corpus is exactly double.** `~/.claude/projects`
holds 96 entries but only 91 distinct roots: `-Users-…-Obsidian-private-personal` is a
**symlink** to `-Users-…-Obsidian-Personal`, so every transcript is reachable under two
paths. Measured 2026-10-16: a naive scan reports 34 rows for 17 distinct menus. The dirs
are collapsed by `os.path.realpath` before any file is opened.

⚠️ **A manager verdict is read from the transcript, not the session registry.** The
registry holds only the sessions still alive (38 of them, measured 2026-10-16, against
3,186 transcripts touched that day), so a historical corpus cannot be classified from it.
A transcript is self-describing — it carries its own `agent-name` and `agent-color`
records — so the session name and colour are read from the file itself. The RULE is not
re-derived: the entry is handed to the guard's own `manager_arm`, so there is exactly one
home for "what is a manager" and this script cannot drift from the hook it grades.

⚠️ **The class labels are the operator's, and they are an input, not a guess.** Whether an
option is *manager-owned* (the manager's own work, which it must do rather than offer) or
*operator-only* (a decision only the operator can make, which must reach them) is a
reading of the Manager Session runbook's § Gate triage class-A line — *"The line is work
vs management — not read vs write"*. This script never assigns that label; it joins the
labels from `--labels` and reports. `--emit-labels` prints a skeleton over the corpus so
the assignment is mechanical to make and reviewable in one place.

Usage:
    manager-menu-corpus.py --from 2026-10-16 --to 2026-10-16 --labels <path>
    manager-menu-corpus.py --from 2026-10-16 --emit-labels     # skeleton to fill in
    manager-menu-corpus.py --list                              # the corpus, unlabelled

Exit codes: 0 the corpus was built (whatever the verdicts) · 2 a required input is
missing or unreadable. ⚠️ A failing SC1 verdict is **not** an error exit — the numbers
are the result, and a scanner that exited non-zero on a red corpus could not be read.
"""
import argparse
import collections
import glob
import importlib.util
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(module_name, filename):
    """Import a sibling script by path. They are shipped side by side, not installed."""
    spec = importlib.util.spec_from_file_location(module_name, os.path.join(_HERE, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The guard owns the predicate AND the manager rule. Both are imported rather than
# reimplemented: a second copy of either would drift from the hook, and the drift would
# be silent — this scanner would report a corpus the deployed guard never sees.
guard = _load("manager_menu_guard", "manager-menu-guard.py")

# The 8 manager-owned labels `# Impact` names, and the set SC1 requires the corpus to
# contain. Kept here rather than in the labels file because it is the criterion's own
# number, not a property of any one corpus.
REQUIRED_MANAGER_OWNED = (
    "Approve the row only",
    "Un-hold the gate row and open it",
    "Create SC2 guards task",
    "Approve SC2 row, hold open",
    "Relay go to pane 1895",
    "Re-scope Map Sentry Project SC2",
    "Graphics task",
    "File a task for goal SC2",
)


# ⚠️ **Only the template's own recommendation marker is stripped — never a trailing
# parenthetical in general.** Measured 2026-10-16 over 79 distinct corpus labels: 35 end
# in `(Recommended)` and 3 in `(rec)`, which are the menu template's marker; but `(a)`,
# `(b)` and `(likely)` also occur and are part of the label's own text. A blanket strip
# would silently merge two genuinely distinct options into one row, which is the failure
# the count cannot show.
_RECOMMENDATION_RE = re.compile(r"\s*\((?:recommended|rec)\)\s*$", re.IGNORECASE)


def normalize_label(label):
    """The label with the menu template's trailing recommendation marker removed.

    Applied on BOTH sides of the containment test: `# Impact` quotes the labels without
    the marker, the transcripts carry it, and comparing the two raw forms reports every
    marked label as missing.
    """
    return _RECOMMENDATION_RE.sub("", (label or "").strip()).strip()


def unique_project_dirs(root=None):
    """Every transcript project dir once, symlinked duplicates collapsed.

    ⚠️ `realpath`, not the entry name. The `-private-personal` entry is a symlink to
    `-Personal`, so comparing names keeps both and doubles every count downstream.
    """
    root = root or guard.projects_dir()
    seen, out = set(), []
    for path in sorted(glob.glob(os.path.join(root, "*"))):
        if not os.path.isdir(path):
            continue
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        out.append(real)
    return out


def last_agent_name(path):
    """The last `agent-name` record's value in `path`, or `None`.

    Mirrors the guard's `last_colour` for what counts as a record — the quoted type
    **value**, never `"type":"agent-name"`, because the latter assumes compact separators
    and a serialization change upstream would silently report every session as unnamed.
    Unlike `last_colour` this reads forwards: the name is one short record, not a value
    appended thousands of times, so the bounded tail read buys nothing here.
    """
    name = None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"agent-name"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("type") != "agent-name":
                    continue
                value = record.get("agentName")
                if isinstance(value, str) and value.strip():
                    name = value
    except OSError:
        return None
    return name


def session_cwd(path):
    """The transcript's own `cwd`, or `None`.

    Read so the entry handed to the guard resolves its transcript the same way the live
    hook does — `transcript_path` prefers the cwd-derived directory and only globs as a
    fallback, and taking the glob for a live session is a coin flip over the two
    symlinked copies.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"cwd"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                value = record.get("cwd")
                if isinstance(value, str) and value.strip():
                    return value
    except OSError:
        return None
    return None


def extract_menus(path):
    """`[(timestamp, [label, ...]), ...]` — one entry per `AskUserQuestion` call.

    ⚠️ Only `assistant` entries are read. The same tool name also appears in `attachment`
    entries, which are the rendered gate records rather than the call itself; counting
    those would double the corpus with rows that carry no labels to grade.
    """
    menus = []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "AskUserQuestion" not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("type") != "assistant":
                    continue
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_use" or block.get("name") != "AskUserQuestion":
                        continue
                    labels = guard.option_labels(block.get("input"))
                    if labels:
                        menus.append((record.get("timestamp"), labels))
    except OSError:
        return []
    return menus


def in_window(timestamp, start, end):
    """Whether an ISO timestamp falls inside `[start, end]` inclusive, by date.

    ⚠️ Compared on the date prefix rather than parsed. Every transcript timestamp is
    `YYYY-MM-DDTHH:MM:SS(.sss)Z`, so the prefix orders lexicographically, and a parse
    would have to decide what to do with a malformed one — where the honest answer is
    "exclude it" and the cheap one is the same.
    """
    if not isinstance(timestamp, str) or len(timestamp) < 10:
        return False
    day = timestamp[:10]
    if start and day < start:
        return False
    if end and day > end:
        return False
    return True


def build_corpus(start, end, projects_root=None):
    """`(menus, stats)` — every manager menu in the window, with its option verdicts.

    `menus` carries one row per option: `(timestamp, session_id, arm, label, verb)` where
    `verb` is the base manager-owned verb the predicate finds, or `None` when the guard
    would allow it. The verdict is per-OPTION, not per-menu: the guard denies a menu when
    any option is manager-owned, but SC1 grades the options, and a menu-level roll-up
    cannot show that a specific operator-only option survived.
    """
    topics, goals = guard.vault_page_names()
    page_titles = tuple(topics.values()) + tuple(goals.values())

    stats = collections.Counter()
    menus = []
    for directory in unique_project_dirs(projects_root):
        stats["project_dirs"] += 1
        for path in sorted(glob.glob(os.path.join(directory, "*.jsonl"))):
            stats["transcripts"] += 1
            found = [m for m in extract_menus(path) if in_window(m[0], start, end)]
            if not found:
                continue
            session_id = os.path.basename(path)[:-6]
            name = last_agent_name(path)
            entry = {"sessionId": session_id, "name": name, "cwd": session_cwd(path)}
            arm = guard.manager_arm(entry, topics, goals)
            if arm is None:
                stats["non_manager_sessions_with_menus"] += 1
                continue
            stats["manager_sessions"] += 1
            for timestamp, labels in found:
                stats["manager_menus"] += 1
                for label in labels:
                    verb = guard.names_manager_owned_action(label, page_titles)
                    menus.append((timestamp, session_id, arm, label, verb))
    return menus, stats


def load_labels(path):
    """`(classes, required)` from the labels file, or `(None, None)` when unreadable.

    Shape:
        {"labels": {"<option label>": "manager-owned" | "operator-only"},
         "required_manager_owned": ["<label>", ...]}   # optional; defaults to the 8
    """
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"unreadable labels file {path}: {exc}", file=sys.stderr)
        return None, None
    if not isinstance(document, dict) or not isinstance(document.get("labels"), dict):
        print(f"labels file {path} carries no 'labels' map", file=sys.stderr)
        return None, None
    required = document.get("required_manager_owned")
    if not isinstance(required, list) or not required:
        required = list(REQUIRED_MANAGER_OWNED)
    return document["labels"], [str(r) for r in required]


def classify(menus, classes):
    """`(verdicts, by_class)` — the SC1 pair, plus the corpus grouped by class.

    ⚠️ An unlabelled option is reported as `unlabelled`, never folded into either class.
    Folding it would let an empty operator-only bucket satisfy the negative control
    vacuously — the corpus would print `0 denied` over a population of nothing, which is
    the failure mode the count alone cannot show.
    """
    by_class = collections.defaultdict(list)
    for timestamp, session_id, arm, label, verb in menus:
        # ⚠️ An empty string is unlabelled, not a third class. `--emit-labels` writes `""`
        # for every row, so without this an unfilled skeleton would bucket everything
        # under a class the report never prints and report `0` unlabelled while nothing
        # had been classified — the one reading the unlabelled count exists to prevent.
        assigned = (classes.get(label) or "").strip()
        by_class[assigned or "unlabelled"].append((timestamp, label, verb))

    manager_owned = by_class["manager-owned"]
    operator_only = by_class["operator-only"]
    verdicts = {
        "manager_owned_total": len(manager_owned),
        "manager_owned_allowed": sum(1 for _, _, verb in manager_owned if verb is None),
        "operator_only_total": len(operator_only),
        "operator_only_denied": sum(1 for _, _, verb in operator_only if verb is not None),
        "unlabelled_total": len(by_class["unlabelled"]),
    }
    return verdicts, by_class


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="start", help="earliest date, YYYY-MM-DD (inclusive)")
    parser.add_argument("--to", dest="end", help="latest date, YYYY-MM-DD (inclusive)")
    parser.add_argument("--labels", help="labels file; required unless --emit-labels or --list")
    parser.add_argument("--emit-labels", action="store_true",
                        help="print a skeleton labels file over the corpus and exit")
    parser.add_argument("--list", action="store_true",
                        help="print the corpus unlabelled and exit")
    parser.add_argument("--projects", help="override the transcripts root (testing)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args(argv)

    menus, stats = build_corpus(args.start, args.end, args.projects)

    if args.list or args.emit_labels:
        seen = collections.OrderedDict()
        for _, _, _, label, verb in menus:
            seen.setdefault(label, verb)
        if args.emit_labels:
            print(json.dumps(
                {"required_manager_owned": list(REQUIRED_MANAGER_OWNED),
                 "labels": {label: "" for label in seen}},
                indent=2, ensure_ascii=False))
        else:
            for label, verb in seen.items():
                print(f"{'deny' if verb else 'allow':5}  {label}")
        return 0

    if not args.labels:
        print("--labels is required unless --emit-labels or --list is given", file=sys.stderr)
        return 2
    classes, required = load_labels(args.labels)
    if classes is None:
        return 2

    verdicts, by_class = classify(menus, classes)
    manager_owned_set = {label for _, label, _ in by_class["manager-owned"]}
    normalized = {normalize_label(label) for label in manager_owned_set}
    missing = [label for label in required if normalize_label(label) not in normalized]

    if args.json:
        print(json.dumps({"stats": dict(stats), "verdicts": verdicts,
                          "missing_required": missing,
                          "manager_owned_set": sorted(manager_owned_set)},
                         indent=2, ensure_ascii=False))
        return 0

    print(f"window: {args.start or '(open)'} .. {args.end or '(open)'}")
    print(f"project dirs (symlink-deduped): {stats['project_dirs']}")
    print(f"transcripts read: {stats['transcripts']}")
    print(f"manager sessions with menus: {stats['manager_sessions']}")
    print(f"non-manager sessions with menus (excluded): {stats['non_manager_sessions_with_menus']}")
    print(f"manager menus: {stats['manager_menus']}")
    print()
    print("corpus by class:")
    for name in ("manager-owned", "operator-only", "unlabelled"):
        print(f"  {name}: {len(by_class[name])}")
    print()
    print("manager-owned label set:")
    for label in sorted(manager_owned_set):
        print(f"  - {label}")
    print()
    print("SC1 verdicts:")
    print(f"  manager-owned options allowed: {verdicts['manager_owned_allowed']}   (required: 0)")
    print(f"  operator-only options denied:  {verdicts['operator_only_denied']}   (required: 0)")
    print(f"  manager-owned set contains the {len(required)} named in # Impact: "
          f"{'yes' if not missing else 'NO'}")
    for label in missing:
        print(f"    missing: {label}")
    if verdicts["unlabelled_total"]:
        print(f"  ⚠️ unlabelled options: {verdicts['unlabelled_total']} "
              f"— the corpus is not fully classified and neither verdict is complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
