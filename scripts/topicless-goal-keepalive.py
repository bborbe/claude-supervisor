#!/usr/bin/env python3
"""The fleet tier's keep-alive for goal managers whose goal no topic declares.

Why this exists
---------------
`docs/session-tiers.md` assigns the fleet manager *"every topic manager, and every goal with no
topic"*. The goal-branch keep-alive (`topic-keepalive.py --branch goal`) ran only inside a
topic manager's round, so a goal manager whose goal no topic declares was resumed by nobody
when it died, and the operator found the lapse by looking. Measured 2026-11-08 on
private-personal: 29 non-terminal goals carried a manager id and were declared by no topic.

What it does
------------
Selects the population and asks `topic-keepalive.py` for each verdict. It **decides, it never
acts**: the fleet round (`commands/steps/keepalive.md`) runs the printed command, then records
the resume with `topic-keepalive.py --record-resume`. Running this script performs no restart,
no open and no cap write, which is what makes it safe for a worker to run as a read-only check.

Population: every goal page that `crashed-managers.py`'s `fleet_keeps` accepts, which means
`status: in_progress`, `phase: execution`, a `claude_session_id`, and either no topic page
whose `## Goals` list names it, or only topics whose own manager reads dead or `cannot-tell`.
The predicate lives in `crashed-managers.py`, one home, because that script must treat the
same set as covered. Otherwise it would card a manager this leg has just restarted.

⚠️ **A goal a topic declares is selected ONLY when that topic's manager is itself unkept —
dead (`resume`) or `cannot-tell`.** Otherwise the topic round owns it: the fleet leg restarts
the topic manager, and the topic manager then restarts the goal manager. Taking the goal here
too would make a second keeper, and so a second writer.

⚠️ **The exception exists because nothing else covers it.** A topic manager that is dead or
undecidable is not restarted by any round — the topic leg's `cannot-tell` acts on nothing —
so every goal manager under it is nobody's population. Measured 2026-11-18 on private-personal:
`selected=22 actionable=0` from the old guard, and the declaring topic's probe returning
`cannot-tell  session 6f14c290… → UNKNOWN; an undecidable probe is never death`.
`topic-keepalive.py`'s `O_EXCL` claim is the double-writer guard for the race that exception
opens: two writers reading `ABSENT` for one `claude_session_id` cannot both resume it.

Output, one block:

  selected: <n>
  <goal>\t<verdict>\t<command or ->\t<topic-keepalive.py's reason line>
  ...
  actionable: <n>        rows whose verdict is `resume` or `restart`

The command column is the action the round takes:

  resume   → /supervisor:open --goal "<goal>"
  restart  → /supervisor:restart <claude_session_id>
  anything else → -   (jump / leave / cannot-tell / crash-loop: nothing to run)

Exit codes: 0 ran (any population, including 0) · 2 REFUSED: an unreadable goals or topics
directory, page, or helper. A `2` prints its reason to stderr and must never be read as
"nothing to resume".
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import os
import sys
from contextlib import redirect_stdout

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


#: The verdicts this leg acts on. Every other `topic-keepalive.py` verdict maps to no command.
ACTING = ("resume", "restart")

_CM = None


class Refused(Exception):
    """A read or a load that could not be taken. Never rendered as an empty population."""


def crashed():
    """`scripts/crashed-managers.py` loaded as a module. It is the home of `fleet_keeps`.

    Loaded lazily and guarded, for the same reason as its own `helpers()`. A broken helper
    must exit 2 with a reason, never raise out of the import and exit 1.
    """
    global _CM
    if _CM is None:
        path = os.path.join(REPO, "scripts", "crashed-managers.py")
        try:
            spec = importlib.util.spec_from_file_location("crashed_managers", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 — any load failure is a refusal
            raise Refused(f"cannot load {path}: {exc}") from exc
        missing = [a for a in ("fleet_keeps", "helpers", "Refused", "_listdir", "_read_page")
                   if not hasattr(module, a)]
        if missing:
            raise Refused(f"{path} is missing {', '.join(missing)} — a broken install")
        _CM = module
    return _CM


def folders(vault: str) -> tuple[str, str]:
    """`(goals_dir, topics_dir)` for THIS vault, absolute, from `resolve-folders.py --vault`.

    Matched by the vault's path, never the first config entry carrying the key: on the
    operator's machine that entry is `private-starcitizen` (`23 Goals`), wrong for
    private-personal (measured 2026-11-08).
    """
    path = os.path.join(REPO, "scripts", "resolve-folders.py")
    try:
        spec = importlib.util.spec_from_file_location("resolve_folders", path)
        rf = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rf)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = rf.main(["--vault", vault, "--require", "goals_dir,topics_dir"])
    except Exception as exc:  # noqa: BLE001 — any failure is a refusal
        raise Refused(f"cannot resolve folders via {path}: {exc}") from exc
    line = out.getvalue().strip()
    if rc != 0 or line.startswith("UNKNOWN"):
        raise Refused(f"folders: {line or 'resolve-folders.py printed nothing'}")
    topics, goals = line.split("|")[0:2]
    return goals, topics


def keepalive():
    """`scripts/topic-keepalive.py` as a module — the same instance `crashed-managers` loaded."""
    try:
        return crashed().helpers()
    except crashed().Refused as exc:
        raise Refused(str(exc)) from exc


#: Topic-manager verdicts that mean no round will restart it, so its declared goals fall to
#: the fleet tier. `restart` is deliberately absent: that session is live with a lapsed loop,
#: and § 1 of the same round restarts it, after which the topic round resumes its goals.
UNKEPT_TOPIC_VERDICTS = ("resume", "cannot-tell")


def topic_verdict(vault: str, topics_dir: str, topic: str) -> str:
    """`topic-keepalive.py --branch topic`'s verdict word for one topic page."""
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            keepalive().main(["--vault", vault, "--subject", topic, "--branch", "topic",
                              "--dir", topics_dir])
    except SystemExit:
        return "cannot-tell"
    except Exception:  # noqa: BLE001 — an unreadable topic is undecidable, never dead
        return "cannot-tell"
    lines = out.getvalue().splitlines()
    return lines[0].strip() if lines else "cannot-tell"


def unkept_topics(vault: str, topics_dir: str) -> dict[str, str]:
    """`topic name → verdict` for every topic whose manager no round will restart."""
    cm = crashed()
    try:
        entries = sorted(e[: -len(".md")] for e in cm._listdir(os.path.join(vault, topics_dir))
                         if e.endswith(".md"))
    except cm.Refused as exc:
        raise Refused(str(exc)) from exc
    out = {}
    for name in entries:
        word = topic_verdict(vault, topics_dir, name)
        if word in UNKEPT_TOPIC_VERDICTS:
            out[name] = word
    return out


def select(vault: str, goals_dir: str, topics_dir: str,
           unkept: dict[str, str] | None = None) -> list[str]:
    """The goal names this leg keeps, sorted. Raises Refused on an unreadable directory."""
    cm = crashed()
    try:
        names = sorted(e[: -len(".md")] for e in cm._listdir(os.path.join(vault, goals_dir))
                       if e.endswith(".md"))
        return [n for n in names
                if cm.fleet_keeps(vault, n, goals_dir, topics_dir, set(unkept or {}))]
    except cm.Refused as exc:
        raise Refused(str(exc)) from exc


def session_id(vault: str, goals_dir: str, name: str) -> str:
    cm = crashed()
    h = keepalive()
    return h.scalar(h.frontmatter(cm._read_page(os.path.join(vault, goals_dir, name + ".md"))),
                    "claude_session_id")


def verdict(vault: str, goals_dir: str, name: str) -> tuple[str, str]:
    """`(verdict, reason)` from `topic-keepalive.py`, run in-process without `--record-resume`."""
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            keepalive().main(["--vault", vault, "--subject", name, "--branch", "goal",
                              "--dir", goals_dir])
    except SystemExit:
        # argparse refused its own arguments. That is a fault in this caller, so report it
        # as undecidable, never as a resume.
        return "cannot-tell", "topic-keepalive.py refused its arguments"
    except Exception as exc:  # noqa: BLE001 — an unreadable page or state dir is undecidable
        # `topic-keepalive.main` opens the goal page and creates its state dir unguarded. An
        # OSError escaping here would end the run with exit 1, a code this script does not
        # define, and drop every later row. One goal's failure is that goal's `cannot-tell`.
        return "cannot-tell", f"topic-keepalive.py raised {type(exc).__name__}: {exc}"
    lines = out.getvalue().splitlines()
    if not lines:
        return "cannot-tell", "topic-keepalive.py printed nothing"
    return lines[0].strip(), (lines[1].strip() if len(lines) > 1 else "")


def command(verdict_word: str, name: str, sid: str) -> str:
    if verdict_word == "resume":
        return f'/supervisor:open --goal "{name}"'
    if verdict_word == "restart" and sid:
        return f"/supervisor:restart {sid}"
    return "-"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault", required=True, help="the vault ROOT path")
    ap.add_argument("--goals-dir", help="override the goals directory (default: the vault's config)")
    ap.add_argument("--topics-dir", help="override the topics directory (default: the vault's config)")
    a = ap.parse_args(argv)
    vault = os.path.abspath(os.path.expanduser(a.vault))
    if not (a.goals_dir and a.topics_dir):
        try:
            goals, topics = folders(vault)
        except Refused as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
        a.goals_dir = a.goals_dir or goals
        a.topics_dir = a.topics_dir or topics

    try:
        cm = crashed()
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    try:
        cm._MEMO.clear()  # the memo is per RUN
        unkept = unkept_topics(vault, a.topics_dir)
        names = select(vault, a.goals_dir, a.topics_dir, unkept)
        rows = []
        for name in names:
            word, reason = verdict(vault, a.goals_dir, name)
            # The reason carries the declaring topic's own verdict, so a round log shows the
            # DECISION (why this goal fell to the fleet tier) rather than an inference, and a
            # leg that hardcoded `cannot-tell` would be caught by a dead-topic fixture.
            for topic in cm.declaring_topics(vault, name, a.topics_dir):
                if topic in unkept:
                    reason = f"declaring topic {topic} reads {unkept[topic]}; {reason}"
                    break
            rows.append((name, word, command(word, name, session_id(vault, a.goals_dir, name)), reason))
    except (Refused, cm.Refused) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    print(f"selected: {len(rows)}")
    for row in rows:
        print("\t".join(row))
    print(f"actionable: {sum(1 for _, word, _, _ in rows if word in ACTING)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
