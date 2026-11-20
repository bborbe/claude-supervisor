#!/usr/bin/env python3
"""PreToolUse guard: refuse a hand-run `wezterm cli spawn` that breaks the role rules.

The wrapper owns every open decision (bborbe/claude-wrapper, `POST /api/1.0/open` and
the `open_rows` MCP tool). This hook is the backstop for a session that hand-rolls a
spawn anyway — the shape that produced 13 agent rows as tabs in the Hold window on
2026-11-18.

Two arms, both on `Bash` calls carrying `wezterm cli spawn`:

1. `--resume <sid>` where that session's task says `role: agent`. Matrix Rule 2: an
   agent never opens a tab, whatever its stored `mode:` says.
2. `--window-id N` where N is the role map's `Hold.window_id`. Hold is the operator's
   parking lot; a spawn into it holds a spawn-cap slot while doing no work.

Fail-open on every error. A guard that failed closed on a parse error would block
every Bash call in every session that loads this plugin, and the endpoint is the real
enforcement — this only catches the hand-run path.

Kill switch: SUPERVISOR_SPAWN_ROUTE_GUARD=off.
"""
import json
import os
import re
import sys

STATE_DIR = os.path.expanduser("~/.claude/state/spawn-route-guard")
ROLE_MAP = os.path.expanduser("~/.cache/wezterm-role-map.json")
VAULT_ROOT = os.path.expanduser("~/Documents/Obsidian")
TASK_DIRS = ("25 Tasks", "24 Tasks", "23 Goals", "24 Goals")
OPEN_ENDPOINT = "POST http://127.0.0.1:18090/api/1.0/open"


#: Growth is bounded by a trim, not by hope. This hook fires on EVERY Bash tool call
#: fleet-wide, so an unbounded append is a slow disk leak — the sibling
#: `manager-menu-guard.py` bounds its own record the same way, and `hooks/hooks.json`
#: documents the bound as required. A session's file is trimmed back to its most recent
#: RECORD_KEEP lines once it passes RECORD_CAP_BYTES.
RECORD_CAP_BYTES = 262144
RECORD_KEEP = 200


def _trim(path):
    """Keep the most recent RECORD_KEEP lines once `path` passes RECORD_CAP_BYTES.

    Written through a temporary file and `os.replace`d, so a kill mid-trim leaves either
    the old file or the new one — never a half-written one. Never raises.
    """
    try:
        if os.path.getsize(path) <= RECORD_CAP_BYTES:
            return
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.writelines(lines[-RECORD_KEEP:])
        os.replace(tmp, path)
    except Exception:
        pass


def record(session_id, decision, reason):
    """One JSON line per invocation, on allow and deny alike. Never raises."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        path = os.path.join(STATE_DIR, "%s.jsonl" % (session_id or "unknown"))
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "ts": __import__("datetime").datetime.now().astimezone().isoformat(),
                        "session_id": session_id,
                        "decision": decision,
                        "reason": reason,
                    }
                )
                + "\n"
            )
        _trim(path)
    except Exception:
        pass


def hold_window_id():
    """The role map's Hold window id, or None. A missing map is not a violation."""
    with open(ROLE_MAP, encoding="utf-8") as handle:
        hold = (json.load(handle) or {}).get("Hold") or {}
    value = hold.get("window_id")
    return int(value) if value is not None else None


def role_for_session(session_id):
    """The RESOLVED `role:` of the task recording `session_id`, or None when no task records it.

    ⚠️ **A task that records the session but omits `role:` returns `agent`, not None.** Matrix
    Rule 1 makes an absent `role:` an agent, so returning None here let the very case this hook
    exists to refuse — a roleless agent row resumed as a tab — pass as "no task found". The two
    answers are different: None means "no task claims this session", `agent` means "this is an
    agent row".

    A bounded scan of the vault task dirs: the hook has 5 s, so it walks the known
    dirs only and stops at the first match.
    """
    needle = "claude_session_id: %s" % session_id
    for vault in sorted(os.listdir(VAULT_ROOT)):
        for name in TASK_DIRS:
            folder = os.path.join(VAULT_ROOT, vault, name)
            if not os.path.isdir(folder):
                continue
            for entry in sorted(os.listdir(folder)):
                if not entry.endswith(".md"):
                    continue
                try:
                    with open(os.path.join(folder, entry), encoding="utf-8") as handle:
                        head = handle.read(4000)
                except OSError:
                    continue
                if needle not in head:
                    continue
                for line in head.splitlines():
                    if line.startswith("role:"):
                        resolved = line.split(":", 1)[1].strip().strip('"').strip("'")
                        return resolved or "agent"
                # The task records this session but declares no role: Rule 1 makes it agent.
                return "agent"
    return None


def verdict(command, session_id):
    """(decision, reason) — "deny" only when an arm actually fires."""
    if not re.search(r"wezterm\s+cli\s+spawn", command):
        return "allow", "not-a-spawn"

    window = re.search(r"--window-id[\s=]+(\d+)", command)
    if window:
        try:
            hold = hold_window_id()
        except Exception:
            hold = None
        if hold is not None and int(window.group(1)) == hold:
            return "deny", "hold-window:%d" % hold

    resume = re.search(r"--resume[\s=]+([0-9a-fA-F-]{8,})", command)
    if resume:
        try:
            role = role_for_session(resume.group(1))
        except Exception:
            role = None
        if role == "agent":
            return "deny", "agent-tab:%s" % resume.group(1)[:8]

    return "allow", "no-arm-fired"


def main():
    if os.environ.get("SUPERVISOR_SPAWN_ROUTE_GUARD") == "off":
        record(os.environ.get("CLAUDE_CODE_SESSION_ID"), "allow", "kill-switch")
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command") or ""
    session_id = payload.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID")

    try:
        decision, reason = verdict(command, session_id)
    except Exception as exc:  # fail-open, and say why in the record
        record(session_id, "allow", "fail-open:%s" % type(exc).__name__)
        return 0

    record(session_id, decision, reason)
    if decision != "deny":
        return 0

    if reason.startswith("hold-window"):
        detail = (
            "the role map's Hold window is the operator's parking lot; a spawn into it "
            "holds a spawn-cap slot while doing no work"
        )
    else:
        detail = (
            "role: agent never opens a tab — role × mode matrix Rule 2 — whatever the "
            "row's stored mode: says"
        )
    sys.stderr.write(
        "Refused: %s.\n"
        "Route the open through the wrapper instead: %s\n"
        "(or the `open_rows` MCP tool). It resolves role→mode, runs the answer-path "
        "probe and picks the window; a hand-run `wezterm cli spawn` is the shape that "
        "opened 13 agent rows into Hold on 2026-11-18.\n" % (detail, OPEN_ENDPOINT)
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
