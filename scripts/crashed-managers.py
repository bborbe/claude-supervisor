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
when either of these holds:

  * `branch == "topic"` — no keep-alive reaches a topic manager at all;
  * `branch == "goal"` and **no live topic manager** declares the goal.

A goal manager under a live topic manager is **covered** and yields no card.

⚠️ **`branch == "fleet"` is counted and reported but never carded, and that is not a gap.**
Nothing sits above a fleet manager, so it is uncovered in the keep-alive sense — but the
crash this script detects is *a LIVE session whose loop lapsed*, and a fleet record carries
no page to read a `manager_session_id` from (`24 Goals/` and `23 Topics/` hold goal and topic
pages; there is no fleet page). With no session id there is no liveness probe, so a fleet
manager's crash cannot be told from its ordinary exit and must not be carded. Every fleet
record still appears in `--json` with that reason, so the population stays countable.

One home for the predicate
--------------------------
`frontmatter`, `scalar` and `base_slug` live in `scripts/topic-keepalive.py`, which is imported
rather than forked: a second discriminator is a second answer. That file is `# Out of Scope`
for the task that added this one; importing it does not modify it. It is loaded **lazily and
guarded** — a broken helper refuses (exit 2) instead of raising out of import (exit 1), because
`1` reads as "ran, found nothing".

Two probes are this file's own, and each exception has a measured reason:

  * **the cadence probe** — the shared `cadence()` hardcodes `--branch goal`, so it cannot
    answer for a topic or a fleet record. This one takes the branch, bounds the child with a
    timeout, and answers `UNREADABLE` for a probe that could not run — a fact distinct from
    `INVALID`, which is a record that exists and cannot be read.
  * **the session probe** — `session_verdict()` matches the instrument's STDOUT, while
    `session-liveness.py` prints every non-LIVE verdict to STDERR (measured 2026-10-29: a
    genuinely `ABSENT` session answered `UNKNOWN` through the shared helper, so an abandoned
    record was indistinguishable from a probe that could not decide). This one reads the
    instrument's own documented EXIT CODES — `0` LIVE, `1` ABSENT, `2` UNKNOWN, `3` AMBIGUOUS,
    `4` MALFORMED — and bounds the child with a timeout.

Neither is a second discriminator: the vocabulary and its meaning stay the instruments'.

Reads fail closed
-----------------
An unreadable `24 Goals/` or `23 Topics/` folder, an unreadable page, or an unreadable state
dir is REFUSED (exit 2), never read as "no page". Reading an unreadable input as empty would
classify every subject as an abandoned record and print a clean "no crashed managers" over a
directory nothing could open.

    --json   print the whole computation, post nothing (the diagnosis form)
    --post   post at most one card per uncovered subject

Exit codes: `0` ran, nothing to do · `0` ran and posted · `2` REFUSED (unreadable state dir,
vault folder, page or board) — a `2` prints its own reason to stderr and must never be read as
"no crashed managers".
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GOALS_DIR = "24 Goals"
TOPICS_DIR = "23 Topics"
STATE_DIR = os.path.expanduser(
    os.environ.get("MANAGER_LIVENESS_STATE_DIR", "~/.claude/state/sweep-gate")
)
CARD_PREFIX = "fleet-crashed-manager:"
CARD_OPTIONS = ("restart it", "not now")
CARD_RECOMMEND = "restart it"

# `manager-liveness.py --state` reads files only; 30 s is a wedged-child bound, not a budget.
PROBE_TIMEOUT = 30
# `attention-ask.py` bounds its own store call at 10 s; this is the outer bound on the child.
POST_TIMEOUT = 60

# The states `manager-liveness.py --state` can answer, plus this file's own probe failure.
CADENCE_STATES = ("OK", "STALE", "STOPPED", "UNARMED", "INVALID")
PROBE_FAILED = "UNREADABLE"

# `session-liveness.py --check`'s documented exit codes. The instrument's contract, not a
# re-derivation: it prints LIVE on stdout and every other verdict on stderr, so the exit code is
# the only channel that carries all five.
SESSION_VERDICTS = {0: "LIVE", 1: "ABSENT", 2: "UNKNOWN", 3: "AMBIGUOUS", 4: "MALFORMED"}


class Refused(Exception):
    """A read that could not be taken. Never rendered as "nothing to do"."""


_KA = None


def helpers():
    """`scripts/topic-keepalive.py` loaded as a module — the filename is not importable.

    ⚠️ Lazy and guarded, and both halves are load-bearing. Loading it at import time meant a
    broken or missing helper raised out of the import and Python exited `1` — this script's
    *ran, found nothing* code — so a fleet tick read a broken discriminator as "no crashed
    managers". A refusal here reaches `main`'s handler and exits `2` with a reason instead.
    """
    global _KA
    if _KA is None:
        path = os.path.join(REPO, "scripts", "topic-keepalive.py")
        try:
            spec = importlib.util.spec_from_file_location("topic_keepalive", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 — any load failure is a refusal, never a crash
            raise Refused(f"cannot load {path}: {exc}") from exc
        # ⚠️ A loaded module is not a usable one. A renamed or re-signatured helper would raise
        # AttributeError/TypeError at a call site, which escapes `main`'s `except Refused` and
        # exits 1 — this script's *ran, found nothing* code. Validate the three here instead.
        missing = [a for a in ("base_slug", "scalar", "frontmatter") if not hasattr(module, a)]
        if missing:
            raise Refused(f"{path} is missing {', '.join(missing)} — a broken install, not an empty fleet")
        _KA = module
    return _KA


#: The `--state-dir` this run was given, forwarded to the child probe. Without it a caller
#: running `--state-dir X` enumerated X and then classified each subject from the DEFAULT
#: store — two different readings in one run, silently.
_STATE_DIR_OVERRIDE: str | None = None

_MEMO: dict = {}

#: `(branch, name) -> the --state detail` (`age=…s limit=…s period=…`). A side channel
#: rather than a second return value so every caller that stubs `cadence_state` keeps working;
#: an absent entry simply omits the age from the card.
_DETAIL: dict = {}


def _memo(key, compute):
    """`compute()` once per run. The cache is cleared at the start of `main`."""
    if key not in _MEMO:
        _MEMO[key] = compute()
    return _MEMO[key]


def _listdir(directory: str) -> list[str]:
    """`os.listdir`, REFUSED rather than empty — an unreadable folder is not an empty one."""

    def read() -> list[str]:
        try:
            return os.listdir(directory)
        except OSError as exc:
            raise Refused(f"cannot read {directory}: {exc}") from exc

    return _memo(("listdir", directory), read)


def _read_page(path: str) -> str:
    """A page's text, REFUSED rather than skipped — a page that cannot be read is not silent."""

    def read() -> str:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            raise Refused(f"cannot read {path}: {exc}") from exc

    return _memo(("page", path), read)


def _page_session_id(front: dict) -> str:
    """The session id a goal or topic page records.

    `manager_session_id` is read first and `claude_session_id` second: the two are being
    unified onto the latter, so reading the retired key first keeps a not-yet-migrated page
    resolving exactly as it did before the writer swap, and the second lookup answers once
    the migration lands.
    """

    scalar = helpers().scalar
    return scalar(front, "manager_session_id") or scalar(front, "claude_session_id")


def subjects(state_dir: str) -> tuple[list[tuple[str, str]], int]:
    """Every `(branch, base)` carrying a `<branch>--<base>.cadence` record, plus the legacy count.

    ⚠️ **Only the prefixed name is read, and the legacy count is returned rather than
    dropped.** `manager-liveness.py` writes `goal--<base>` / `topic--<base>` / `fleet--<base>`;
    a flat pre-split `<base>.cadence` carries no branch, so it cannot be attributed here and
    is an *abandoned record* by construction — never a card. Counting it keeps "42 subjects"
    from being read as the whole population when the directory holds 88.
    """
    found = []
    legacy = 0
    for entry in _listdir(state_dir):
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
    for entry in _listdir(directory):
        if entry.endswith(".md") and helpers().base_slug(entry[: -len(".md")]) == base:
            return os.path.join(directory, entry)
    return None


def goals_section(text: str) -> list[str]:
    """The lines under the page's `## Goals` heading, up to the next `## ` heading.

    ⚠️ **This is the only region a declaration counts in, and the narrowness is the point.**
    A whole-file substring match also matches a `# Progress` entry, a quoted comment or an
    unrelated section that merely *mentions* the goal — and every such match made a goal look
    covered by a topic manager whose keep-alive never touches it, which suppresses a real card.
    A page with no `## Goals` heading declares nothing. ⚠️ **Any heading ends the section,
    whatever its level** — a `# Progress` immediately after `## Goals` is a different section.
    """
    lines: list[str] = []
    inside = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            inside = stripped.lower() == "## goals"
            continue
        if inside:
            lines.append(line)
    return lines


def declares(text: str, goal: str) -> bool:
    """Does this topic page list `[[<goal>]]` in its goals list?"""
    # ⚠️ **Only a list entry declares.** A `## Goals` section routinely carries prose recording a
    # goal's REMOVAL, and a section-wide `[[…]]` scan reads that retraction as a declaration —
    # `agents/topic-verify.md` § parse_widths names this defect and measured it four times on
    # 2026-09-20. Over-matching here makes a goal look covered and suppresses its real card, so
    # only a `-`/`*` bullet whose FIRST wikilink is the goal counts.
    #
    # Obsidian resolves `[[link]]` case-insensitively, so `[[my goal]]` against `My Goal.md` DOES
    # declare it; a case-sensitive test would card a covered manager.
    wanted = goal.casefold()
    for line in goals_section(text):
        if not line.lstrip().startswith(("- ", "* ")):
            continue
        start = line.find("[[")
        if start == -1:
            continue
        ends = [i for i in (line.find("]]", start), line.find("|", start), line.find("#", start)) if i != -1]
        if not ends:
            continue
        target = line[start + 2 : min(ends)].strip().casefold()
        if target == wanted:
            return True
    return False


def session_state(sid: str) -> str:
    """The instrument's verdict for this session id, memoized — one probe per id per run.

    Read from the exit code, not the text: `session-liveness.py` prints `LIVE` on stdout and
    `ABSENT` / `UNKNOWN` / `AMBIGUOUS` / `MALFORMED` on stderr, so a stdout-only reader can only
    ever answer `LIVE` or `UNKNOWN` and an abandoned record stays invisible.

    ⚠️ **Only `LIVE` can card**, and an unreadable probe is `UNKNOWN` — never `ABSENT`, which is
    a verdict about a session rather than about the probe. A child that dies with an
    undocumented code is `UNKNOWN` too; a traceback exits `1`, which the instrument reserves for
    `ABSENT`, and both are no-card either way.
    """

    def probe() -> str:
        try:
            done = subprocess.run(
                [sys.executable, os.path.join(REPO, "scripts", "session-liveness.py"),
                 "--check", sid],
                capture_output=True, text=True, timeout=PROBE_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError):
            return "UNKNOWN"
        return SESSION_VERDICTS.get(done.returncode, "UNKNOWN")

    return _memo(("session", sid), probe)


def topic_manager_live(vault: str, goal_name: str) -> bool:
    """Is a live topic manager responsible for this goal?

    Every topic page is read once per run (memoized by path), so a vault with N goals and M
    topic pages costs M page reads, not N x M.
    """
    directory = os.path.join(vault, TOPICS_DIR)
    for entry in _listdir(directory):
        if not entry.endswith(".md"):
            continue
        text = _read_page(os.path.join(directory, entry))
        if not declares(text, goal_name):
            continue
        sid = _page_session_id(helpers().frontmatter(text))
        if not sid or session_state(sid) != "LIVE":
            continue
        # ⚠️ A LIVE session is NOT a running keep-alive. `/supervisor:stop` disarms the loop and
        # leaves the session alive, so a STOPPED topic manager reads LIVE while restarting
        # nothing — and treating it as coverage would suppress a real goal-manager crash. The
        # topic's own cadence is the only thing that says the loop is actually running.
        topic_stem = entry[: -len(".md")]
        if cadence_state(topic_stem, "topic") in ("STOPPED", "STALE", "UNARMED", PROBE_FAILED):
            continue
        return True
    return False


def cadence_state(name: str, branch: str) -> str:
    """`manager-liveness.py --state` for one subject, via this file's own probe.

    ⚠️ **The child is bounded, and a probe that cannot answer is `UNREADABLE`, not `INVALID`.**
    `INVALID` is `manager-liveness.py`'s own verdict for a record that EXISTS and cannot be
    read — a fact about the subject. A crashed or timed-out probe is a fact about the probe,
    and folding it into `INVALID` made "the instrument did not run" indistinguishable from
    "this record is corrupt". Neither is `STALE`, so neither cards; the difference is what the
    `--json` diagnosis says, which is the only place a human reads it.
    """

    def probe() -> str:
        try:
            env = None
            if _STATE_DIR_OVERRIDE:
                env = {**os.environ, "MANAGER_LIVENESS_STATE_DIR": _STATE_DIR_OVERRIDE}
            done = subprocess.run(
                [sys.executable, os.path.join(REPO, "scripts", "manager-liveness.py"),
                 "--state", "--subject", name, "--branch", branch],
                capture_output=True, text=True, timeout=PROBE_TIMEOUT, env=env,
            )
        except (OSError, subprocess.SubprocessError):
            return PROBE_FAILED
        if done.returncode != 0:
            return PROBE_FAILED
        parts = (done.stdout.splitlines() or [""])[0].split("\t")
        state = parts[0].strip()
        if len(parts) > 2:
            _DETAIL[(branch, name)] = parts[2].strip()
        return state if state in CADENCE_STATES else PROBE_FAILED

    return _memo(("cadence", branch, name), probe)


def evaluate(vault: str, branch: str, base: str) -> dict:
    """One subject's verdict. `card` is true only for an uncovered, live, lapsed manager."""
    row = {"branch": branch, "base": base, "card": False, "reason": "", "session": "", "state": ""}
    if branch == "fleet":
        row["reason"] = (
            "fleet record — nothing sits above a fleet manager, but it has no page to carry a "
            "manager_session_id, so live-but-lapsed cannot be established and it is never carded"
        )
        return row

    directory = os.path.join(vault, TOPICS_DIR if branch == "topic" else GOALS_DIR)
    subject_page = page(directory, base)
    if not subject_page:
        row["reason"] = f"no page under {directory} — an abandoned record, never a card"
        return row

    name = os.path.basename(subject_page)[: -len(".md")]
    row["subject"] = name
    row["state"] = cadence_state(name, branch)
    if row["state"] == PROBE_FAILED:
        row["reason"] = "cadence probe could not answer — a probe failure is never a lapse"
        return row
    if row["state"] != "STALE":
        row["reason"] = f"cadence {row['state']} — only STALE is a lapse (STOPPED is a stand-down)"
        return row

    sid = _page_session_id(helpers().frontmatter(_read_page(subject_page)))
    if not sid:
        row["reason"] = "no session id on the page — unverifiable, never abandoned"
        return row
    row["session"] = sid
    row["detail"] = _DETAIL.get((branch, name), "")

    verdict = session_state(sid)
    if verdict != "LIVE":
        row["reason"] = f"session {verdict} — not a live-but-lapsed manager"
        return row

    if branch == "goal" and topic_manager_live(vault, name):
        row["reason"] = "covered — a live topic manager's keep-alive restarts it"
        return row

    row["card"] = True
    row["reason"] = "live but lapsed, and no keep-alive covers it"
    return row


def post(row: dict) -> str:
    """One card per subject. Returns the store's line, or raises Refused.

    ⚠️ **The dedup key carries the BRANCH.** A goal and a topic may share a name — that is why
    `manager-liveness.py` prefixes its state files — so a key built from the subject alone let
    one card suppress the other, and the suppressed subject's crash was never raised.
    """
    dedup = f"{CARD_PREFIX}{row['branch']}--{row['base']}"
    age = f" ({row['detail']})" if row.get("detail") else ""
    payload = (
        f"manager lapsed: {row['subject']} [{row['branch']}] — session {row['session'][:8]} is "
        f"live but its loop stopped{age}, and no keep-alive covers it"
    )
    try:
        probe = subprocess.run(
            [sys.executable, os.path.join(REPO, "scripts", "attention-ask.py"), "post",
             "--dedup-key", dedup, "--payload", payload,
             "--option", CARD_OPTIONS[0], "--option", CARD_OPTIONS[1],
             "--recommend", CARD_RECOMMEND, "--producer-kind", "manager"],
            capture_output=True, text=True, timeout=POST_TIMEOUT,
        )
    except subprocess.SubprocessError as exc:
        raise Refused(f"attention-ask.py did not answer within {POST_TIMEOUT}s: {exc}") from exc
    except OSError as exc:
        raise Refused(f"attention-ask.py could not be run: {exc}") from exc
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
    global _STATE_DIR_OVERRIDE
    _STATE_DIR_OVERRIDE = os.path.abspath(os.path.expanduser(a.state_dir))
    a.state_dir = _STATE_DIR_OVERRIDE  # the parent's read must use the same path as the child
    _MEMO.clear()  # the memo is per RUN, so a second call in one process starts clean
    _DETAIL.clear()
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
