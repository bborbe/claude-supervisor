#!/usr/bin/env python3
"""Prove the tab channel end to end, in one run.

Drives server/supervisor.mjs over stdio and checks the WORKER'S OWN TRANSCRIPT rather
than the server's self-report:

  1. spawn a tab worker                 -> does the colour actually apply?
  2. send it a follow-up message        -> does the worker act on it?

Both answers come from the transcript file, because "the spawn returned success" is
exactly the claim that was wrong before: the old code reported a coloured worker that
had never been coloured.
"""
import json, os, select, subprocess, sys, time

# The repo root, derived from this file's own location. A hardcoded home path works on
# exactly one machine and breaks silently everywhere else.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(REPO, "server", "supervisor.mjs")
# NOT derived from the requested cwd. The cc-* launcher does `cd "$OBSIDIAN_PERSONAL"`,
# so a worker spawned with cwd=/tmp runs in the vault and writes its transcript there —
# which also means the spawn response's transcript_dir points somewhere the transcript
# never lands. Verified by the registry entry for a /tmp spawn showing the vault cwd.
# The one machine-specific value left, and it is data rather than a repo path: it names
# THIS operator's vault, because that is where the cc-* launcher cd's and therefore where
# the transcript lands. There is no generic derivation — a different vault means a
# different directory — so it stays explicit rather than pretending to be portable.
PROJECT_DIR = os.path.expanduser("~/.claude/projects/-Users-user-Documents-Obsidian-my-vault")

env = dict(os.environ)
env["ANTHROPIC_BASE_URL"] = env.get("CLAUDE_CODE_ROUTER_URL", "http://127.0.0.1:8788")

before = set(os.listdir(PROJECT_DIR)) if os.path.isdir(PROJECT_DIR) else set()

proc = subprocess.Popen(["node", SERVER], cwd=REPO, env=env, stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
rid = [0]


def call(name, args, timeout=180):
    rid[0] += 1
    mine = rid[0]
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": mine, "method": "tools/call",
                                 "params": {"name": name, "arguments": args}}) + "\n")
    proc.stdin.flush()
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            return {"error": "server closed stdout"}
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if msg.get("id") == mine:
            text = msg.get("result", {}).get("content", [{}])[0].get("text", "{}")
            try:
                return json.loads(text)
            except Exception:
                return {"raw": text}
    return {"error": "timeout"}


def transcript_entries(path):
    out = []
    try:
        for line in open(path):
            line = line.strip()
            if line.startswith("{"):
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return out


def newest_transcript():
    now = set(os.listdir(PROJECT_DIR)) if os.path.isdir(PROJECT_DIR) else set()
    fresh = sorted(now - before, key=lambda f: os.path.getmtime(os.path.join(PROJECT_DIR, f)))
    return os.path.join(PROJECT_DIR, fresh[-1]) if fresh else None


def wait_for(predicate, seconds=60):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(1)
    return False


proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                        "clientInfo": {"name": "channel-drill", "version": "0"}}}) + "\n")
proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
proc.stdin.flush()
time.sleep(2)
while select.select([proc.stdout], [], [], 0)[0]:
    proc.stdout.readline()

print("1. spawning a tab worker...")
spawned = call("spawn_agent", {
    "prompt": "Reply with exactly: FIRST. Use no tools.",
    "cwd": "/tmp", "label": "channel-drill", "interactive": True,
})
print("   ->", json.dumps(spawned))
agent_id = spawned.get("agent_id")
if not agent_id:
    print("FAIL: no agent spawned"); proc.kill(); sys.exit(1)

path = wait_for(lambda: newest_transcript() is not None, 30) and newest_transcript()
print(f"\n2. worker transcript: {path}")

colour_ok = wait_for(lambda: any(
    "Session color set to" in json.dumps(e) for e in transcript_entries(path)), 30) if path else False
print(f"   colour applied: {colour_ok}")

print("\n3. sending a follow-up message...")
sent = call("send_agent_message", {"agent_id": agent_id, "message": "Now reply with exactly: SECOND. Use no tools."})
print("   ->", json.dumps(sent))

acted = wait_for(lambda: any(
    "SECOND" in json.dumps(e.get("message", {}).get("content", "")) or "SECOND" in json.dumps(e)
    for e in transcript_entries(path)), 90) if path else False
print(f"   worker acted on it: {acted}")

print()
ok = bool(colour_ok and sent.get("sent") and acted)
print("RESULT:", "PASS — colour applied and the follow-up was acted on" if ok
      else f"FAIL — colour={colour_ok} sent={sent.get('sent')} acted={acted}")
print(f"tab/pane: {spawned.get('pane_id')} — close with: wezterm cli kill-pane --pane-id {spawned.get('pane_id')}")
proc.kill()
sys.exit(0 if ok else 1)
