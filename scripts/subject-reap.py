#!/usr/bin/env python3
"""Reap a manager whose SUBJECT has reached a terminal state.

Why this exists
---------------
The reap contract (`agents/fleet-drive.md` step 1b) reads three facts from a **task** file.
A manager has no task file: its subject is a goal or a topic page, and the only record of
the manager on it is the page's session id. So a manager bound to a subject that finished
never matches the task reap, and it outlives the work it was started for. Measured
2026-10-30: `Codex Drives a Permanently Running Claude Session` read `status: completed`,
`phase: done`, carried a manager session id, and that session was `LIVE` with its loop
`STOPPED` — idle in that state for 31 minutes before anyone looked.

Nothing was wrong with the session. The **binding** was stale, and no reader joined a
subject page's terminal status to its session id and then asked whether that session is
still live. This script is that join.

The two halves, and the order between them
------------------------------------------
⚠️ **`manager_session_id` is a lead, never a verdict.** Subject terminality decides
*candidacy*; liveness decides *what to do with the candidate*; neither alone is enough.
The order is load-bearing and is `commands/steps/goal-resolve.md`'s rule, applied here:

  * a page whose `status` is not terminal is **not scanned at all** — no probe, no row.
  * a page with no session id is `silent` — a completed goal that never recorded a
    manager is not a reap case (its own defect lives elsewhere).
  * a page whose id the probe reads `ABSENT` or `MALFORMED` is `silent` — the manager
    already ended, or the value could not have named one; either way the reap is moot.
  * `UNKNOWN` / `AMBIGUOUS` is `unknown` — reported, never acted on. Same fail-closed
    split the rest of the plugin uses: a probe that could not run is not a probe that
    found nothing.
  * `LIVE` is the only reapable verdict.

Non-authorising, by construction
--------------------------------
This script kills nothing and writes no session state. It emits the same evidence
hand-over the task reap emits (`scripts/reap-line.py`, subject form), and the session ends
by its own `/supervisor:close` — or `/exit` when it has no task anchor. There is no
`os.kill`, no `signal`, no write under the supervisor state directory, and no write to the
page: a leg that could close the manager itself would be a different, riskier change.

Which page field carries the manager
------------------------------------
`claude_session_id` is the current name; `manager_session_id` is the retired one that
`commands/open.md` § Step 2B/2C still reads as a fallback. Both are read, current first,
because a page carrying both is a page mid-migration and the newer value is the one the
manager-resolution path acts on.

Output
------
One pipe-separated line per row, so a probe can assert on the first field, plus the reap
lines themselves — those are what the leg pastes into its `REAPS` block:

    ✅ REAP: <subject> [<sid8>] — subject <kind> status: <status> · ...
    REAP     | <vault> | <kind> | <subject> | <session> | LIVE
    SILENT   | <vault> | <kind> | <subject> | <reason>
    UNKNOWN  | <vault> | <kind> | <subject> | <reason>
    CARD     | <dedup key> | <subject> | <session>
    SUMMARY  | terminal=<n> reap=<n> silent=<n> unknown=<n>

`SUMMARY reap=<n>` is the regression metric's reading: **managers with a terminal subject**
must read 0 at the end of every round. A reading above 0 for two consecutive rounds means
this leg stopped running or stopped matching, and it is the only signal that separates
"reaped" from "never looked".

`--json` prints the same computation as one object and nothing else.

Exit codes
----------
  0  the scan ran (whether or not anything is reapable)
  2  REFUSED — a vault root, a goals/topics directory, or the vault set could not be read.
     Never read as "nothing to reap": an unreadable directory and a clean one must not
     render the same.
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION_LIVENESS = os.path.join(HERE, "session-liveness.py")
ATTENTION_ASK = os.path.join(HERE, "attention-ask.py")

# The scheduling states that make a subject a reap candidate. `hold` is deliberately NOT
# here: a held subject is paused, not finished, and its manager may legitimately wait on
# it — a hold blocks *opening* a manager but must not be read as *closing* one.
TERMINAL_STATUSES = ("completed", "aborted")

# The page field the manager is recorded in, current first. See the module docstring.
MANAGER_KEYS = ("claude_session_id", "manager_session_id")

# `propose-managers.py` resolves a vault's goals dir from the vault-cli config; a bare
# `--vault-root` has no config entry, so the same two spellings are tried here.
GOAL_DIRS = ("24 Goals", "23 Goals")

# `session-liveness.py --check` exit codes — the vocabulary this script branches on.
LIVE, ABSENT, UNKNOWN, AMBIGUOUS, MALFORMED = 0, 1, 2, 3, 4
VERDICT_WORD = {
    LIVE: "LIVE",
    ABSENT: "ABSENT",
    UNKNOWN: "UNKNOWN",
    AMBIGUOUS: "AMBIGUOUS",
    MALFORMED: "MALFORMED",
}

# Only these are silence rather than a report: the manager already ended (`ABSENT`), or the
# value could not have named one (`MALFORMED`). `UNKNOWN`/`AMBIGUOUS` are "cannot tell" and
# are reported — never acted on, and never silently dropped either.
SILENT_CODES = (ABSENT, MALFORMED)

#: Seconds one liveness probe may take. One probe per terminal page, serially, so this is
#: the per-page bound on the leg's whole run; a timeout reads as `unknown`, never a verdict.
PROBE_TIMEOUT = 30


def _load(name, filename):
    """Import a sibling script as a module — never a copy of its rules."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pm = _load("propose_managers", "propose-managers.py")
reap = _load("reap_line", "reap-line.py")


def roots_from_args(raw_roots):
    """`--vault-root` dirs resolved to `{name, goals_dir, topics_dir}`, or None.

    ⚠️ **A typo'd path must not read as a clean scan.** A missing root is REFUSED (exit 2),
    the same answer `approved-not-started.py` gives, because "no terminal subject" and
    "I could not look" must never render the same.
    """
    out = []
    for raw in raw_roots:
        path = os.path.abspath(os.path.expanduser(raw))
        if not os.path.isdir(path):
            print(f"UNKNOWN — vault root not found: {path}", file=sys.stderr)
            return None
        goals = next((os.path.join(path, d) for d in GOAL_DIRS
                      if os.path.isdir(os.path.join(path, d))), None)
        topics = next((os.path.join(path, d) for d in pm.TOPIC_DIRS
                       if os.path.isdir(os.path.join(path, d))), None)
        if goals is None and topics is None:
            # ⚠️ **A root with neither directory would scan nothing and report `terminal=0`** —
            # a clean-looking round for a vault that has no subjects at all, which is
            # indistinguishable from a healthy one. Refuse instead.
            print(f"UNKNOWN — no goals or topics directory under: {path}", file=sys.stderr)
            return None
        out.append({
            "name": os.path.basename(path),
            "goals_dir": goals,
            "topics_dir": topics,
        })
    return out


def page_rows(directory):
    """`(title, block, path)` per page — or None when the directory cannot be read.

    ⚠️ **`None` is "unreadable", never "empty".** A goals dir that returned `[]` for a read
    failure would render as a clean round with nothing to reap — the exact
    "a suppressed leg and a leg that found nothing must never print the same" failure.
    A vault with no such directory at all is legitimate and returns `[]`.
    """
    rows, unreadable = [], []
    if not directory:
        return rows, unreadable
    # ⚠️ **A configured-but-absent directory is EMPTY, not unreadable.** `vaults()` takes
    # `goals_dir` straight from the vault config without checking it exists, so a stale
    # config entry would otherwise refuse the whole round every tick. An EXISTING directory
    # that cannot be listed is still `None` — "absent" and "I could not look" stay distinct.
    if not os.path.exists(directory):
        return rows, unreadable
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return None, None
    for filename in names:
        if not filename.endswith(".md"):
            continue
        path = os.path.join(directory, filename)
        try:
            with open(path, encoding="utf-8") as handle:
                block = pm.frontmatter(handle.read())
        except (OSError, UnicodeDecodeError) as error:
            # ⚠️ **Never a silent drop.** An unreadable page could be a terminal subject
            # carrying a live manager, so skipping it would suppress exactly the reap this
            # leg exists for. It is reported as `unknown` — never acted on, never hidden.
            unreadable.append((filename[:-3], path, str(error)))
            continue
        if block:
            rows.append((filename[:-3], block, path))
    return rows, unreadable


def manager_id(block):
    """`(id, field name)` for the manager recorded on a subject page, or `(None, None)`.

    Current name first, retired name second — see the module docstring. An empty value is
    an absent one: `claude_session_id:` with nothing after it is the same fact as no key.
    """
    for key in MANAGER_KEYS:
        value = (pm.field(block, key) or "").strip().strip('"').strip("'")
        if value:
            return value, key
    return None, None


def subjects(roots):
    """Every terminal subject page in every root, or None when a directory is unreadable."""
    out = []
    for vault in roots:
        for kind, key in (("goal", "goals_dir"), ("topic", "topics_dir")):
            rows, unreadable = page_rows(vault.get(key))
            if rows is None:
                return None
            for name, path, error in unreadable:
                out.append({
                    "vault": vault["name"], "kind": kind, "subject": name,
                    "path": path, "status": "", "session": None, "field": None,
                    "verdict": "unknown", "reason": "page unreadable: %s" % error,
                })
            for name, block, path in rows:
                status = (pm.field(block, "status") or "").strip().strip('"').strip("'").lower()
                if status not in TERMINAL_STATUSES:
                    continue
                session, field_name = manager_id(block)
                out.append({
                    "vault": vault["name"],
                    "kind": kind,
                    "subject": name,
                    "path": path,
                    "status": status,
                    "session": session,
                    "field": field_name,
                })
    return out


def probe(session_id, endpoint=None, registry_dir=None):
    """`session-liveness.py --check`'s exit code, or None when the probe could not run."""
    cmd = [sys.executable, SESSION_LIVENESS, "--check", session_id]
    if endpoint:
        cmd += ["--endpoint", endpoint]
    if registry_dir:
        cmd += ["--dir", registry_dir]
    try:
        # ⚠️ **Bounded.** One probe per terminal page, run serially, so a store that stops
        # answering would otherwise hang the whole round on the first subject. A timeout is
        # "could not tell" (unknown), never a verdict about the session.
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode


def roster(endpoint=None, registry_dir=None):
    """`{session id: roster name}` from `session-liveness.py --list --json`, or None.

    ⚠️ **This is what makes the reap ADDRESSABLE.** `SendMessage` takes the exact roster
    name, not the session id, and a manager's name is not derivable from its subject page —
    the page title, the subject title and the session name are three different strings.
    One read per run, bounded like the probe; `None` means "could not read the roster", and
    a row then keeps its id, which the caller must report as unaddressable rather than
    guess a name from.
    """
    cmd = [sys.executable, SESSION_LIVENESS, "--list", "--json"]
    if endpoint:
        cmd += ["--endpoint", endpoint]
    if registry_dir:
        cmd += ["--dir", registry_dir]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        records = json.loads(proc.stdout)
    except ValueError:
        return None
    if not isinstance(records, list):
        return None
    return {(r.get("sessionId") or "").lower(): (r.get("name") or "")
            for r in records if isinstance(r, dict)}


def classify(subject, endpoint=None, registry_dir=None, names=None):
    """The row's verdict: `reap`, `silent` or `unknown`, with the reason it was decided from."""
    row = dict(subject)
    if row.get("verdict"):
        # A row the scan already decided — an unreadable page. Never probed, never re-judged.
        return row
    if not row["session"]:
        row.update(verdict="silent", reason="no manager_session_id recorded")
        return row
    code = probe(row["session"], endpoint, registry_dir)
    if code is None:
        row.update(verdict="unknown", reason="liveness probe could not run")
        return row
    word = VERDICT_WORD.get(code, "exit %s" % code)
    row["liveness"] = word
    if code == LIVE:
        row.update(verdict="reap", reason="liveness: %s" % word)
        if names is not None:
            row["roster"] = names.get(row["session"].lower()) or None
    elif code in SILENT_CODES:
        row.update(verdict="silent", reason="liveness: %s" % word)
    else:
        row.update(verdict="unknown", reason="liveness: %s" % word)
    return row


def card_key(vault, subject):
    """The dedup key for the reap card — one per (vault, subject), so a later round re-posts nothing.

    ⚠️ **The vault is part of the key.** Goal and topic titles are not namespaced across
    vaults, so a key on the title alone would make two same-titled subjects in different
    vaults collide — and the second reap would be suppressed by the first one's card.
    """
    return "manager-subject-reap:%s:%s" % (vault, subject)


def post_card(row, producer_kind=None, producer_id=None, timeout=30):
    """Post the reap's attention card (`ack` — it reports, it asks nothing).

    ⚠️ **`ack`, never `message`.** A reap needs no decision from the operator: the session
    closes itself and the card exists so the reap is visible, not so it can be approved.
    A `message` card here would manufacture an operator gate the leg has no question for.

    Best-effort by design: a failed post returns None and the reap line still stands,
    because trading a missing notification for a skipped reap is the wrong way round.
    """
    cmd = [
        sys.executable, ATTENTION_ASK, "post",
        "--dedup-key", card_key(row["vault"], row["subject"]),
        "--mechanism", "ack",
        "--payload",
        'Reaped the manager of "%s": subject %s is %s and its manager session %s was '
        'LIVE, so it was told to close itself.' % (
            row["subject"], row["kind"], row["status"], row["session"]),
        "--context",
        "%s page: %s. Non-authorising — no process was killed and no state was written; "
        "the session ends through /supervisor:close." % (row["kind"], row["path"]),
    ]
    if producer_kind:
        cmd += ["--producer-kind", producer_kind]
    if producer_id:
        cmd += ["--producer-id", producer_id]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        print("⚠️ card post failed for %s: %s" % (row["subject"], error), file=sys.stderr)
        return None
    if proc.returncode != 0:
        detail = (proc.stderr.strip() or proc.stdout.strip() or "").splitlines()
        print("⚠️ card post refused for %s: %s"
              % (row["subject"], detail[-1] if detail else "exit %s" % proc.returncode),
              file=sys.stderr)
        return None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("ITEM_ID:"):
            return line[len("ITEM_ID:"):].strip()
    return None


def render(rows):
    """The reap lines first, then one pipe-separated row each, then the SUMMARY.

    The reap lines lead because they are the deliverable: the leg pastes them into its
    `REAPS` block verbatim. The pipe rows are the evidence a probe asserts on.
    """
    lines = []
    tally = {"reap": 0, "silent": 0, "unknown": 0}
    for row in rows:
        tally[row["verdict"]] += 1
        if row["verdict"] == "reap":
            lines.append(reap.subject_reap_line(
                row["subject"], row["kind"], row["status"], row["session"], "live",
                roster=row.get("roster")))
    for row in rows:
        if row["verdict"] == "reap":
            lines.append("REAP | %s | %s | %s | %s | %s" % (
                row["vault"], row["kind"], row["subject"], row["session"],
                row.get("liveness", "")))
        else:
            lines.append("%s | %s | %s | %s | %s" % (
                row["verdict"].upper(), row["vault"], row["kind"], row["subject"],
                row["reason"]))
    lines.append("SUMMARY | terminal=%d reap=%d silent=%d unknown=%d" % (
        len(rows), tally["reap"], tally["silent"], tally["unknown"]))
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Reap a manager whose goal or topic subject is terminal.")
    parser.add_argument("--vault-root", action="append", default=[],
                        help="a vault root to scan (repeatable); default: every configured vault")
    parser.add_argument("--endpoint", default=None,
                        help="override the attention store base URL (passed to session-liveness.py)")
    parser.add_argument("--registry-dir", default=None,
                        help="override the identity registry path (passed to session-liveness.py)")
    parser.add_argument("--post-cards", action="store_true",
                        help="post the one ack card per reaped subject")
    parser.add_argument("--producer-id", default=None, help="with --post-cards: the card's producer")
    parser.add_argument("--json", action="store_true",
                        help="print the whole computation as JSON and nothing else")
    args = parser.parse_args(argv)

    roots = roots_from_args(args.vault_root) if args.vault_root else pm.vaults()
    if roots is None:
        print("UNKNOWN — the vault set could not be read; nothing was scanned", file=sys.stderr)
        return 2

    scanned = subjects(roots)
    if scanned is None:
        print("UNKNOWN — a goals or topics directory could not be read; the subject set is "
              "unknown, never empty", file=sys.stderr)
        return 2

    names = roster(args.endpoint, args.registry_dir)
    rows = [classify(subject, args.endpoint, args.registry_dir, names) for subject in scanned]

    if args.post_cards:
        for row in rows:
            if row["verdict"] == "reap":
                row["card"] = post_card(row, producer_id=args.producer_id)

    if args.json:
        json.dump({
            "rows": [
                {
                    "vault": row["vault"], "kind": row["kind"], "subject": row["subject"],
                    "path": row["path"], "status": row["status"],
                    "session": row["session"], "field": row["field"],
                    "verdict": row["verdict"], "reason": row["reason"],
                    "liveness": row.get("liveness"),
                    "card": row.get("card"),
                    "roster": row.get("roster"),
                    "line": (reap.subject_reap_line(
                        row["subject"], row["kind"], row["status"], row["session"], "live",
                        roster=row.get("roster"))
                        if row["verdict"] == "reap" else None),
                }
                for row in rows
            ],
            "count": len(rows),
            "reap": sum(1 for row in rows if row["verdict"] == "reap"),
        }, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    for line in render(rows):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
