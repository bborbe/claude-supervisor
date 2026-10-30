#!/usr/bin/env python3
"""Resume a session that has NO registry entry -- the resumer the reopen rule needs.

`restart-worker.py` is kill-first: it resolves its target from
`~/.claude/sessions/<pid>.json` and refuses `unknown-session-id` BEFORE its
hold/status/kind gates. That is correct for what it does, and it is the wrong
instrument for this job — every session the drive leg's reopen rule selects is
registry-absent by construction (`orphan-candidates.py` drops the live set;
`drive-retained.py` emits `orphan` only for a session it reads as absent). Measured
2026-10-30 on `bborbe/claude-supervisor#524`: the rule identified `f9792011` correctly
and then had nothing that could bring it back.

There is nothing to kill and no pid to look up, so this script skips straight to the
resume recipe — the same one `commands/open.md` Step 3.1 and `restart-worker.py` carry,
reused here through `resume_command()` rather than copied a third time.

**The cwd is recovered from the transcript.** The resume recipe needs the session's
original working directory, and the registry entry that used to hold it is gone. Every
transcript under `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl` carries a `cwd`
field on its first message record, so the directory name is never decoded (that
encoding is lossy — a real `-` in a path component is indistinguishable from a `/`).
The field is read, not the name.

Refusal tokens, first line of stdout, each exit 1:

  malformed-id         the argument is not a single session id
  already-live         the session is LIVE -- resuming it would be a second writer
  cannot-tell          the liveness probe could not answer; retryable, never terminal
  unknown-transcript   no `~/.claude/projects/*/<id>.jsonl` carries this session
  no-cwd               the transcript carries no readable `cwd`
  launcher-unresolved  no launcher could be resolved for this session
  resume-failed        the spawn ran and did not return a pane id

`resumed` (exit 0) prints the pane id on the second line.

⚠️ **`already-live` is the safety gate, and it is checked first.** A resume onto a live
session is the double-writer failure `session-liveness.py` and `topic-keepalive.py` both
exist to prevent, and it is the one mistake here that loses work rather than time.
⚠️ **`already-live` means LIVE and nothing else.** `UNKNOWN` / `AMBIGUOUS` / `MALFORMED`
are `cannot-tell`, a separate retryable token — folding them into `already-live` would
let one transient probe failure become a terminal refusal the caller records.

Usage:
  resume-session.py --session <id> [--title <text>] [--colour <chip>] [--dry-run]
"""

from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECTS = os.path.expanduser("~/.claude/projects")
SESSION_ID = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")


def load_sibling(filename):
    """Import a sibling script by path — the repo's own convention for sharing code."""
    name = os.path.basename(filename).replace(".py", "").replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def refuse(token, message):
    print(token)
    print(f"  {message}")
    return 1


def liveness(session_id):
    """LIVE / ABSENT / CANNOT-TELL, by EXIT CODE — the printed token is not the contract.

    The real `session-liveness.py` writes `LIVE — …` to stdout and every negative
    verdict to stderr, so matching the text reads cannot-tell for both.
    """
    try:
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "session-liveness.py"), "--check", session_id],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "CANNOT-TELL", str(exc)
    detail = ((proc.stdout or "").strip() or (proc.stderr or "").strip()).splitlines()
    return {0: "LIVE", 1: "ABSENT"}.get(proc.returncode, "CANNOT-TELL"), (detail[0].strip() if detail else "")


def transcript_cwd(session_id):
    """The session's original cwd, read from its transcript. (cwd, reason, token)."""
    paths = sorted(glob.glob(os.path.join(PROJECTS, "*", f"{session_id}.jsonl")))
    if not paths:
        return None, "no transcript under ~/.claude/projects/*/%s.jsonl" % session_id[:8], "unknown-transcript"
    # Newest first: a session resumed before leaves more than one transcript.
    for path in sorted(paths, key=os.path.getmtime, reverse=True):
        try:
            with open(path, encoding="utf-8") as handle:
                for _, line in zip(range(200), handle):
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = record.get("cwd")
                    if isinstance(cwd, str) and cwd.strip():
                        return cwd.strip(), path, ""
        except OSError as exc:
            return None, f"transcript unreadable: {exc}", "no-cwd"
    return None, "the transcript carries no readable `cwd` in its first 200 records", "no-cwd"


def main():
    parser = argparse.ArgumentParser(description="Resume a session that has no registry entry.")
    parser.add_argument("--session", required=True, help="the session id to resume")
    parser.add_argument("--title", default="", help="tab title (default: the session id's first 8)")
    parser.add_argument("--colour", default="pink", help="colour chip")
    parser.add_argument("--dry-run", action="store_true", help="print the command, do not run it")
    args = parser.parse_args()

    sid = args.session.strip()
    if not SESSION_ID.match(sid):
        return refuse("malformed-id", f"{sid!r} is not a single session id")

    verdict, detail = liveness(sid)
    if verdict == "LIVE":
        return refuse("already-live", f"session {sid[:8]} is LIVE ({detail}) -- a resume would be a second writer")
    if verdict != "ABSENT":
        # ⚠️ NOT `already-live`. A probe that could not run is retryable; folding it into
        # `already-live` would let one transient failure become a terminal refusal the
        # caller records, permanently blocking a candidate the rule had just judged
        # resumable. Only a positively LIVE answer is terminal.
        return refuse("cannot-tell", f"liveness: {detail or verdict} -- only ABSENT licenses a resume")

    cwd, source, token = transcript_cwd(sid)
    if cwd is None:
        return refuse(token, source)

    worker = load_sibling("restart-worker.py")
    fb = load_sibling("fleet-board.py")
    launcher, launcher_source = worker.resolve_launcher(sid, cwd, fb)
    if not launcher:
        return refuse("launcher-unresolved", f"no launcher for {sid[:8]}: {launcher_source}")

    command = worker.resume_command(sid, args.title.strip() or sid[:8], args.colour.strip().lower(), cwd, launcher)
    if args.dry_run:
        print("resumed")
        print(f"  cwd {cwd} (from {os.path.basename(os.path.dirname(source))})")
        print(f"  launcher {launcher} ({launcher_source})")
        print(f"  command {command}")
        return 0

    try:
        spawned = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return refuse("resume-failed", f"spawn failed: {exc}")
    if spawned.returncode != 0:
        return refuse("resume-failed", f"exit {spawned.returncode}: {(spawned.stderr or '').strip()}")

    panes = (spawned.stdout or "").strip().splitlines()
    print("resumed")
    print(f"  pane {panes[-1] if panes else '?'}")
    print(f"  cwd {cwd} · launcher {launcher} ({launcher_source})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
