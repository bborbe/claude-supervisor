#!/usr/bin/env python3
"""The one home for "which topics declare this goal".

A goal is declared by the **union** of:

  * its own page's `topics:` frontmatter, and
  * every topic page whose `## Goals` section lists it,

**excluding any topic page whose `status` is terminal (`completed` / `aborted`)**. An archived
topic page runs no manager and contributes nothing, so a goal whose only declaring topic is
archived is treated as topic-less and falls to the fleet tier.

Both readers call this module, so they cannot disagree about who keeps a goal:

  * `keeper-census.py` — `scan()` builds `goal_topics` through `resolve()`.
  * `crashed-managers.py` — `declaring_topics()` delegates here.

⚠️ Loaded by path (`importlib`), because the filename carries hyphens — the same convention as
`owner.py` and `goals-declaration.py`. A broken install raises at the call site and is caught by
each caller's own refusal path, never read as "no declaring topics".
"""

from __future__ import annotations

import os
import re

TERMINAL = {"completed", "aborted"}
TOPIC_DIRS = ("23 Topics", "22 Topics")
GOAL_DIRS = ("24 Goals", "23 Goals")
NULLS = {"", "~", "null", "none", "nil"}


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _listdir(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def frontmatter(text):
    """The frontmatter block, or "" — a page with none carries no `status` or `topics:`."""
    m = re.match(r"---\r?\n(.*?)\r?\n---", text, re.S)
    return m.group(1) if m else ""


def scalar(fm, key):
    # `[ \t]*`, never `\s*`: `\s` eats the newline, so an empty key captured the NEXT key.
    m = re.search(rf"^{key}:[ \t]*(.*)$", fm, re.M)
    if not m:
        return ""
    value = m.group(1).strip().strip('"').strip("'")
    return "" if value.lower() in NULLS else value


def links(fm, key):
    """Wikilink targets of a list key — the goal page's `topics:`."""
    m = re.search(rf"^{key}:[ \t]*(.*(?:\n[ \t-].*)*)", fm, re.M)
    return re.findall(r"\[\[([^\]|#]+)", m.group(1)) if m else []


def goals_section(text):
    """The bullet lines of the `## Goals` section, or [] — any heading ends it.

    ⚠️ **Any heading ends the section, whatever its level** — a `# Progress` immediately after
    `## Goals` is a different section. A page with no `## Goals` heading declares nothing.
    """
    out, inside = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            inside = stripped.lower() == "## goals"
            continue
        if inside:
            out.append(line)
    return out


def declares(text, goal_name):
    """Does this topic page list `[[<goal>]]` as a bullet in its `## Goals` section?

    ⚠️ **Only a list entry declares.** A `## Goals` section routinely carries prose recording a
    goal's REMOVAL, and a section-wide `[[…]]` scan reads that retraction as a declaration.
    Obsidian resolves `[[link]]` case-insensitively, so the comparison casefolds.
    """
    wanted = goal_name.casefold()
    for line in goals_section(text):
        if not line.lstrip().startswith(("- ", "* ")):
            continue
        start = line.find("[[")
        if start == -1:
            continue
        ends = [i for i in (line.find("]]", start), line.find("|", start),
                            line.find("#", start)) if i != -1]
        if not ends:
            continue
        if line[start + 2:min(ends)].strip().casefold() == wanted:
            return True
    return False


def _topic_pages(vault, topics_dir=None):
    """`{topic name: (text, frontmatter)}` for every topic page on disk, terminal or not."""
    out = {}
    dirs = (topics_dir,) if topics_dir else TOPIC_DIRS
    for dirname in dirs:
        directory = os.path.join(vault, dirname)
        for entry in _listdir(directory):
            if not entry.endswith(".md"):
                continue
            text = _read(os.path.join(directory, entry))
            out[entry[:-3]] = (text, frontmatter(text))
    return out


def _goal_text(vault, goal_name, goals_dir=None):
    dirs = (goals_dir,) if goals_dir else GOAL_DIRS
    for dirname in dirs:
        path = os.path.join(vault, dirname, goal_name + ".md")
        if os.path.isfile(path):
            return _read(path)
    return ""


def resolve(vault, goal_name, topics_dir=None, goals_dir=None,
            topic_pages=None, goal_fm=None):
    """`(live_declaring, archived_declaring)`, both sorted.

    `live_declaring` is the rule's answer — the union minus terminal topic pages. The archived
    half is returned for reporting only (a census row names an archived declaring topic rather
    than hiding it behind `NO-PAGE`); no keeper decision may use it.

    `topic_pages` and `goal_fm` let a caller that has already scanned the vault (the census)
    hand its own reads in, so the rule is applied without a second read per goal.
    """
    topics = topic_pages if topic_pages is not None else _topic_pages(vault, topics_dir)
    live, archived = set(), set()
    for name, (text, fm) in topics.items():
        if not declares(text, goal_name):
            continue
        (archived if scalar(fm, "status") in TERMINAL else live).add(name)
    if goal_fm is None:
        goal_fm = frontmatter(_goal_text(vault, goal_name, goals_dir))
    for topic in links(goal_fm, "topics"):
        entry = topics.get(topic)
        if entry is None:
            continue
        (archived if scalar(entry[1], "status") in TERMINAL else live).add(topic)
    return sorted(live), sorted(archived)


def declaring_topics(vault, goal_name, topics_dir=None, goals_dir=None):
    """The rule: the union of the goal's `topics:` and every topic's `## Goals`, minus terminal
    topic pages. Both readers call this, so they cannot disagree."""
    return resolve(vault, goal_name, topics_dir, goals_dir)[0]
