#!/usr/bin/env python3
"""Exercise the two resume refusals through the real tool.

Both are refusals, and they are different refusals — that is the point. A tab worker
cannot honour `resume` at all, while a headless worker can but must not resume a
session that is still running. Conflating them would be easy and would tell the caller
the wrong thing.

  1. interactive:true  + resume  -> refused: the tab path drops the flag
  2. interactive:false + resume  -> refused: that session is still running
  3. interactive:true  (no resume) -> allowed: a plain tab worker is unaffected

Neither refusal spawns anything, so this is safe to run against a live session id.
"""
import json, os, select, subprocess, sys, time

# The repo root, derived from this file's own location. A hardcoded home path works on
# exactly one machine and breaks silently everywhere else.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(REPO, "server", "supervisor.mjs")
# A session that is definitely live: this drill's own caller is not it, so use the
# newest registry entry rather than guessing.
REGISTRY = os.path.expanduser("~/.claude/sessions")
CLOSED_ID = "00000000-0000-4000-8000-000000000000"

env = dict(os.environ)
env["ANTHROPIC_BASE_URL"] = env.get("CLAUDE_CODE_ROUTER_URL", "http://127.0.0.1:8788")

live_id = None
for name in os.listdir(REGISTRY):
    if name.endswith(".json"):
        try:
            live_id = json.load(open(os.path.join(REGISTRY, name))).get("sessionId")
            if live_id:
                break
        except Exception:
            pass

proc = subprocess.Popen(["node", SERVER], cwd=REPO, env=env, stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
rid = [0]


def call(name, args, timeout=120):
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


proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                        "clientInfo": {"name": "resume-tab-drill", "version": "0"}}}) + "\n")
proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
proc.stdin.flush()
time.sleep(2)
while select.select([proc.stdout], [], [], 0)[0]:
    proc.stdout.readline()

print("1. a tab worker asked to resume (a closed id, so only the tab limitation can fire):")
tab = call("spawn_agent", {"prompt": "x", "cwd": "/tmp", "label": "resume-tab-drill",
                           "interactive": True, "resume": CLOSED_ID})
print("   ->", json.dumps(tab.get("error", tab))[:260])
tab_refused = "headless worker only" in (tab.get("error") or "")
tab_spawned = "agent_id" in tab

print("\n2. a headless worker asked to resume a session that is still running:")
live = call("spawn_agent", {"prompt": "x", "cwd": "/tmp", "label": "resume-tab-drill-h",
                            "interactive": False, "resume": live_id})
print(f"   (live id {live_id})")
print("   ->", json.dumps(live.get("error", live))[:260])
live_refused = "still running" in (live.get("error") or "")

print("\n3. a plain tab worker, no resume:")
plain = call("spawn_agent", {"prompt": "Reply with exactly: OK. Use no tools.",
                             "cwd": "/tmp", "label": "resume-tab-drill-plain", "interactive": True})
print("   ->", json.dumps({k: plain.get(k) for k in ("agent_id", "pane_id", "status")}))
plain_ok = bool(plain.get("agent_id"))
if plain.get("pane_id"):
    subprocess.run(["wezterm", "cli", "kill-pane", "--pane-id", str(plain["pane_id"])], capture_output=True)

ok = tab_refused and not tab_spawned and live_refused and plain_ok
print("\nRESULT:", "PASS — the tab refusal fires, the headless liveness refusal still fires, and a plain tab is unaffected"
      if ok else f"FAIL — tab_refused={tab_refused} tab_spawned={tab_spawned} live_refused={live_refused} plain_ok={plain_ok}")
proc.kill()
sys.exit(0 if ok else 1)
