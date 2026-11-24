#!/usr/bin/env python3
"""The topic manager ceiling: may a manager be opened for THIS topic page?

After the goal-manager tier merged into the topic tier (2026-11-24), this script's only
job is the TOPIC tier's own ceiling — the check the `claude-wrapper` open endpoint
(`runTopicCeiling`) runs before a topic create. It answers ONE question, on the
`--topic-open` branch: is the topic flagged, and does the fleet manager ceiling admit
another manager?

  topic-unflagged        the TOPIC page does not carry a `flag: true` the operator set.
  manager-cap            the fleet-wide manager ceiling is reached.

Exit 0 = allowed (prints `open-ok: <topic>`, the line the wrapper matches on). Exit 1 = a
refusal (a named token on the first line of stdout). Exit 2 = usage error, never a verdict.

⚠️ **The `open-ok: <subject>` stdout contract is BYTE-IDENTICAL to what the wrapper has
always matched** — same prefix, same subject echo, same exit codes — because
`bborbe/claude-wrapper` (`pkg/open_tab.go` `runTopicCeiling`) requires stdout to start
with `open-ok: ` + the request subject, and its tests cannot run from this repo. The
`--goal` start gate this script used to carry is gone with the goal-manager tier; only the
topic-open branch remains.
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TOPICS_DIR = "23 Topics"

REFUSE_TOPIC_UNFLAGGED = "topic-unflagged"
REFUSE_CAP = "manager-cap"

#: The one flag predicate — see `scripts/flag_provenance.py`. Loaded by path because this
#: script runs standalone out of the plugin cache, where `scripts/` is not on `sys.path`.
_HERE = os.path.dirname(os.path.abspath(__file__))
_FLAG_SPEC = importlib.util.spec_from_file_location(
    "flag_provenance", os.path.join(_HERE, "flag_provenance.py"))
flag_provenance = importlib.util.module_from_spec(_FLAG_SPEC)
_FLAG_SPEC.loader.exec_module(flag_provenance)


def flagged(fm: str, kind: str = "goal") -> bool:
    """Does this page's flag admit it? Delegates to `flag_provenance.flag_admits()`.

    Provenance IS read: a `flag_set_by` outside `OPENING_ACTORS`, absent included, does not
    open a manager here (operator ruling 2026-11-10, attention card `f8cd8b41…`).
    """
    return flag_provenance.flag_admits(fm, kind)


def refuse(token: str, detail: str, bound: str | None = None) -> int:
    print(token)
    if bound:
        print(bound)
    print(detail)
    return 1


def frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return text[3:end] if end != -1 else ""


def resolve_page(directory: str, name: str) -> str | None:
    """Exact basename, case-insensitive — `docs/subject-resolution.md` § The page test."""
    try:
        entries = os.listdir(directory)
    except OSError:
        return None
    want = name.lower() + ".md"
    for entry in entries:
        if entry.lower() == want:
            return os.path.join(directory, entry)
    return None


def fleet_manager_ceiling() -> int | None:
    """`spawn.maxManagersSoft` as the server resolves it, or None when it cannot be read.

    Both spellings, current first — the same fold `resolveMaxManagers` applies. Reading only
    the current name would return None for an operator's existing config, both `ceiling is
    not None` guards would be skipped, and the fleet ceiling would silently stop binding.
    """
    cfg = os.path.expanduser("~/.config/claude-supervisor/config.json")
    try:
        with open(cfg, encoding="utf-8") as fh:
            spawn = (json.load(fh) or {}).get("spawn") or {}
    except (OSError, ValueError):
        return None
    value = spawn.get("maxManagersSoft")
    if value is None:
        value = spawn.get("maxManagers")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def fleet_manager_count() -> int | None:
    """Live managers fleet-wide, the fleet manager excluded. None when unreadable.

    Read from `fleet-board.py --json`, whose per-session `role` is the classification
    the cap counts — never from a name suffix, which a worker's label can also carry.
    """
    probe = subprocess.run(
        [sys.executable, os.path.join(REPO, "scripts", "fleet-board.py"), "--json"],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        return None
    try:
        doc = json.loads(probe.stdout)
    except ValueError:
        return None
    sessions = doc.get("sessions")
    if not isinstance(sessions, list):
        return None
    return sum(
        1 for s in sessions
        if isinstance(s, dict)
        and s.get("role") == "manager"
        and (s.get("label") or "") != "Fleet Manager"
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault", required=True, help="the vault ROOT path")
    ap.add_argument("--topic", required=True, help="the topic manager's subject")
    ap.add_argument("--topics-dir", default=DEFAULT_TOPICS_DIR)
    ap.add_argument("--topic-open", dest="topic_open", action="store_true",
                    help="may a manager be opened for this TOPIC page itself? (the topic "
                         "tier's flag gate)")
    ap.add_argument("--hand-run", dest="hand_run", action="store_true",
                    help="the operator's own /supervisor:open — skip the flag check, keep "
                         "the manager ceiling")
    a = ap.parse_args(argv)
    if not a.topic_open:
        ap.error("this script only answers --topic-open (the topic manager ceiling)")

    vault = os.path.abspath(os.path.expanduser(a.vault))
    topics_dir = os.path.join(vault, a.topics_dir)

    # The topic page must exist and must actually be a topic page.
    topic_page = resolve_page(topics_dir, a.topic)
    if not topic_page:
        return refuse("topic-not-found", f"no page at {topics_dir}/{a.topic}.md")
    with open(topic_page, encoding="utf-8") as fh:
        topic_text = fh.read()
    if not re.search(r"^page_type:\s*topic\s*$", frontmatter(topic_text), re.MULTILINE):
        return refuse("topic-not-found",
                      f"{topic_page} does not declare `page_type: topic` in its frontmatter")

    # The flag must be operator-set (2026-11-10, attention card f8cd8b41) and the ceiling
    # must admit another manager. ⚠️ `--hand-run` skips the flag check and nothing else
    # (operator ruling 2026-11-15): the operator's own /supervisor:open opens what they
    # name, but the manager ceiling binds a hand-run too — the operator's intent does not
    # create a slot.
    if not a.hand_run and not flagged(frontmatter(topic_text), kind="topic"):
        return refuse(
            REFUSE_TOPIC_UNFLAGGED,
            f"<{a.topic}> is not flagged. Opening a topic manager is an open, and this "
            f"tier only opens what carries the flag — set `flag: true` on the topic page. "
            f"Worker sessions are opened by the wrapper's flagged tick regardless.",
        )
    ceiling = fleet_manager_ceiling()
    count = fleet_manager_count() if ceiling is not None else None
    if ceiling is not None and count is not None and count >= ceiling:
        return refuse(
            REFUSE_CAP,
            f"the fleet manager ceiling is reached ({count} of {ceiling}); start nothing "
            f"this tick and report the bound.",
            "manager-cap",
        )
    print(f"open-ok: {a.topic}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
