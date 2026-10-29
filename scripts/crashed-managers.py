#!/usr/bin/env python3
"""Raise a card for a manager that is LIVE but whose loop has lapsed, where nothing else covers it.

Why this exists
---------------
The fleet round's only manager signal is a *recovery* one: a manager that died and left its
subject unattended. `scripts/propose-managers.py` answered the opposite question — *"which goal
holds approved work nobody opened?"* — and was removed from the round on the operator's ruling
(2026-10-29): *"u should not recommend to open more goals, maybe only crashed managers."*

The detection already exists in two pieces and neither is rebuilt here:

  * `scripts/manager-liveness.py --state` classifies a subject `OK` / `STALE` / `STOPPED` /
    `UNARMED` / `INVALID`. `STALE` is the lapsed loop; `STOPPED` is a deliberate stand-down
    and is **never** a crash.
  * `scripts/session-liveness.py --check <id>` answers whether the manager session still
    exists. `ABSENT` is an abandoned record; `LIVE` with a `STALE` cadence is the crash.

What was missing is the card, and the *population* — because `scripts/topic-keepalive.py`
already restarts lapsed goal managers from the topic tier, uncapped. A card for those would
duplicate an action already being taken. Operator decision, 2026-10-29: **card only where no
keep-alive covers the manager.**

The uncovered population
------------------------
`topic-keepalive.py` is invoked from exactly two places — `commands/topic-drive.md` and
`agents/topic-drive.md` — i.e. by a **topic manager, on its goal managers only** (`--branch
goal`, `goal--<slug>.json`). `docs/subject-resolution.md` states the other half outright:
*nothing restarts a topic manager from a `topic--<slug>` cadence*. So a subject is uncovered
when any of these holds:

  * `branch == "topic"` — no keep-alive reaches a topic manager at all;
  * `branch == "goal"` and **no live topic manager** declares the goal;
  * `branch == "fleet"` — nothing sits above a fleet manager.

A goal manager under a live topic manager is **covered** and yields no card.

One home for the predicate
--------------------------
The four helpers this needs — `frontmatter`, `scalar`, `cadence`, `session_verdict` — already
live in `scripts/topic-keepalive.py`, which is imported rather than forked: a second
discriminator is a second answer. That file is `# Out of Scope` for the task that added this
one; importing it does not modify it.

    --json   print the whole computation, post nothing (the diagnosis form)
    --post   post at most one card per uncovered subject

Exit codes: `0` ran, nothing to do · `0` ran and posted · `2` REFUSED (unreadable state dir or
board) — a `2` prints its own reason to stderr and must never be read as "no crashed managers".
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _keepalive_helpers():
    """`scripts/topic-keepalive.py` loaded as a module — the filename is not importable."""
    path = os.path.join(REPO, "scripts", "topic-keepalive.py")
    spec = importlib.util.spec_from_file_location("topic_keepalive", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ka = _keepalive_helpers()
base_slug = _ka.base_slug
frontmatter = _ka.frontmatter
scalar = _ka.scalar
session_verdict = _ka.session_verdict

GOALS_DIR = "24 Goals"
TOPICS_DIR = "23 Topics"
STATE_DIR = os.path.expanduser(
    os.environ.get("MANAGER_LIVENESS_STATE_DIR", "~/.claude/state/sweep-gate")
)
CARD_PREFIX = "fleet-crashed-manager:"


class Refused(Exception):
    """A read that could not be taken. Never rendered as "nothing to do"."""


def subjects(state_dir: str) -> tuple[list[tuple[str, str]], int]:
    """Every `(branch, base)` carrying a `<branch>--<base>.cadence` record, plus the legacy count.

    ⚠️ **Only the prefixed name is read, and the legacy count is returned rather than
    dropped.** `manager-liveness.py` writes `goal--<base>` / `topic--<base>` / `fleet--<base>`;
    a flat pre-split `<base>.cadence` carries no branch, so it cannot be attributed here and
    is an *abandoned record* by construction — never a card. Counting it keeps "42 subjects"
    from being read as the whole population when the directory holds 88.
    """
    try:
        entries = os.listdir(state_dir)
    except OSError as exc:
        raise Refused(f"cannot read the sweep-gate state dir {state_dir}: {exc}") from exc
    found = []
    legacy = 0
    for entry in entries:
        if not entry.endswith(".cadence"):
            continue
        name = entry[: -len(".cadence")]
        branch, sep, base = name.partition("--")
        if sep and branch in ("goal", "topic", "fleet") and base:
            found.append((branch, base))
        else:
            legacy += 1
    return sorted(found), legacy


def page(directory: str, base: str) -> str | None:
    """The page whose slugified stem is `base` — exact-basename matching, as the docs require."""
    try:
        entries = os.listdir(directory)
    except OSError:
        return None
    for entry in entries:
        if entry.endswith(".md") and base_slug(entry[: -len(".md")]) == base:
            return os.path.join(directory, entry)
    return None


def declares(path: str, goal: str) -> bool:
    """Does this topic page list `[[<goal>]]` under its goals?"""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return False
    return f"[[{goal}]]" in text


def topic_manager_live(vault: str, goal_page: str, goal_name: str) -> bool:
    """Is a live topic manager responsible for this goal?"""
    directory = os.path.join(vault, TOPICS_DIR)
    try:
        entries = os.listdir(directory)
    except OSError:
        return False
    for entry in entries:
        if not entry.endswith(".md"):
            continue
        topic_path = os.path.join(directory, entry)
        if not declares(topic_path, goal_name):
            continue
        with open(topic_path, encoding="utf-8") as fh:
            sid = scalar(frontmatter(fh.read()), "manager_session_id")
        if sid and session_verdict(sid) == "LIVE":
            return True
    return False


def evaluate(vault: str, branch: str, base: str) -> dict:
    """One subject's verdict. `card` is true only for an uncovered, live, lapsed manager."""
    row = {"branch": branch, "base": base, "card": False, "reason": "", "session": "", "state": ""}
    if branch == "fleet":
        row["reason"] = "fleet branch has no page — a fleet record is not attributable to a page"
        return row

    directory = os.path.join(vault, TOPICS_DIR if branch == "topic" else GOALS_DIR)
    subject_page = page(directory, base)
    if not subject_page:
        row["reason"] = f"no page under {directory} — an abandoned record, never a card"
        return row

    name = os.path.basename(subject_page)[: -len(".md")]
    row["subject"] = name
    row["state"] = cadence_state(name, branch)
    if row["state"] != "STALE":
        row["reason"] = f"cadence {row['state']} — only STALE is a lapse (STOPPED is a stand-down)"
        return row

    with open(subject_page, encoding="utf-8") as fh:
        sid = scalar(frontmatter(fh.read()), "manager_session_id")
    if not sid:
        row["reason"] = "no manager_session_id on the page — unverifiable, never abandoned"
        return row
    row["session"] = sid

    verdict = session_verdict(sid)
    if verdict != "LIVE":
        row["reason"] = f"session {verdict} — not a live-but-lapsed manager"
        return row

    if branch == "goal" and topic_manager_live(vault, subject_page, name):
        row["reason"] = "covered — a live topic manager's keep-alive restarts it"
        return row

    row["card"] = True
    row["reason"] = "live but lapsed, and no keep-alive covers it"
    return row


def cadence_state(name: str, branch: str) -> str:
    """`manager-liveness.py --state` for one subject, via the keepalive helper."""
    probe = subprocess.run(
        [sys.executable, os.path.join(REPO, "scripts", "manager-liveness.py"),
         "--state", "--subject", name, "--branch", branch],
        capture_output=True, text=True,
    )
    first = (probe.stdout.splitlines() or [""])[0]
    state = first.split("\t")[0].strip()
    return state if state in ("OK", "STALE", "STOPPED", "UNARMED", "INVALID") else "INVALID"


def post(row: dict) -> str:
    """One card per subject. Returns the store's line, or raises Refused."""
    dedup = CARD_PREFIX + row["subject"].lower()
    payload = (
        f"manager lapsed: {row['subject']} — session {row['session'][:8]} is live but its loop "
        f"stopped, and no keep-alive covers it"
    )
    probe = subprocess.run(
        [sys.executable, os.path.join(REPO, "scripts", "attention-ask.py"), "post",
         "--dedup-key", dedup, "--payload", payload,
         "--option", "restart it", "--option", "not now", "--recommend", "restart it",
         "--producer-kind", "manager"],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        raise Refused(f"attention-ask.py refused: {(probe.stderr or probe.stdout).strip()[:300]}")
    return (probe.stdout or "").strip().splitlines()[-1] if probe.stdout.strip() else "posted"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault", default="~/Documents/Obsidian/private-personal")
    ap.add_argument("--state-dir", default=STATE_DIR)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true", help="print the computation, post nothing")
    mode.add_argument("--post", action="store_true", help="post one card per uncovered subject")
    a = ap.parse_args(argv)

    vault = os.path.abspath(os.path.expanduser(a.vault))
    try:
        pairs, legacy = subjects(a.state_dir)
        rows = [evaluate(vault, branch, base) for branch, base in pairs]
        if a.post:
            for row in rows:
                if row["card"]:
                    row["posted"] = post(row)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    if a.json or not a.post:
        print(json.dumps({"rows": rows, "cards": sum(1 for r in rows if r["card"]),
                          "legacy_records": legacy}, indent=2))
        return 0

    cards = [r for r in rows if r["card"]]
    print(f"crashed-managers: {len(rows)} attributed subject(s), {legacy} legacy record(s) "
          f"(never carded), {len(cards)} card(s)")
    for row in cards:
        print(f"  {row['subject']} [{row['branch']}] session {row['session'][:8]} — {row.get('posted', '')}")
    if not cards:
        print("  (none)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
