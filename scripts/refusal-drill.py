#!/usr/bin/env python3
"""Exercise the two-writer refusal through the real MCP tool, not a unit test.

Drives server/supervisor.mjs over stdio: initialize, spawn a throwaway headless
worker, wait for its session id, then try to resume that id while it is still
running. A correct guard refuses; the target is a throwaway, so a guard failure
damages nothing that matters.
"""
import json, os, subprocess, sys, time

# The repo root, derived from this file's own location. A hardcoded home path works on
# exactly one machine and breaks silently everywhere else.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(REPO, "server", "supervisor.mjs")

env = dict(os.environ)
# Route the worker through claude-code-router; never reach a provider directly.
env["ANTHROPIC_BASE_URL"] = env.get("CLAUDE_CODE_ROUTER_URL", "http://127.0.0.1:8788")

proc = subprocess.Popen(
    ["node", SERVER], cwd=REPO, env=env,
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
)
next_id = [0]

def send(method, params=None):
    next_id[0] += 1
    rid = next_id[0]
    frame = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        frame["params"] = params
    proc.stdin.write(json.dumps(frame) + "\n")
    proc.stdin.flush()
    return rid

def call(name, args):
    rid = send("tools/call", {"name": name, "arguments": args})
    deadline = time.time() + 120
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            return {"error": "server closed stdout"}
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if msg.get("id") == rid:
            text = msg.get("result", {}).get("content", [{}])[0].get("text", "{}")
            try:
                return json.loads(text)
            except Exception:
                return {"raw": text}
    return {"error": "timeout"}

send("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "refusal-drill", "version": "0"}})
proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
proc.stdin.flush()
time.sleep(2)  # let initialize settle; drain anything buffered
import select
while select.select([proc.stdout], [], [], 0)[0]:
    proc.stdout.readline()

print("1. spawning a throwaway headless worker...")
spawned = call("spawn_agent", {
    "prompt": "Run the shell command `sleep 300` and then reply DONE.",
    "cwd": "/tmp",
    "label": "refusal-drill-target",
    "interactive": False,
})
print("   ->", json.dumps(spawned))
agent_id = spawned.get("agent_id")
if not agent_id:
    print("FAIL: no agent spawned"); proc.kill(); sys.exit(1)

print("2. waiting for its session id (init fires before any model call)...")
session_id = None
for _ in range(60):
    st = call("agent_status", {"agent_id": agent_id})
    session_id = st.get("session_id")
    if session_id:
        break
    time.sleep(1)
if not session_id:
    print("FAIL: worker never reported a session id"); proc.kill(); sys.exit(1)
print("   -> session_id:", session_id, " status:", st.get("status"))

print("3. confirming the worker is LIVE by the same probe the guard uses...")
sys.path.insert(0, os.path.join(REPO, "server"))
probe = subprocess.run(
    ["node", "--input-type=module", "-e",
     f"import {{ checkLiveness }} from '{os.path.join(REPO, 'server', 'liveness.mjs')}';"
     f"console.log(JSON.stringify(checkLiveness('{session_id}')))"],
    capture_output=True, text=True, cwd=REPO)
print("   -> new probe:", probe.stdout.strip() or probe.stderr.strip())

# A/B: the probe this replaced, run against the same live session right now.
old = subprocess.run(["pgrep", "-fl", session_id], capture_output=True, text=True)
print("   -> OLD probe (pgrep -fl alone):",
      f"FOUND {old.stdout.strip()!r}" if old.returncode == 0 and old.stdout.strip()
      else "found nothing — the old guard would have ALLOWED this resume")

print("4. attempting to resume that still-running session...")
resumed = call("spawn_agent", {
    "prompt": "this must never run",
    "cwd": "/tmp",
    "label": "refusal-drill-second-writer",
    "interactive": False,
    "resume": session_id,
})
print("   ->", json.dumps(resumed))

err = (resumed or {}).get("error", "")
ok = bool(err) and "still running" in err
print()
print("RESULT:", "PASS — the refusal fired through the real tool" if ok
      else "FAIL — the resume was allowed; two writers now exist on the throwaway")
proc.kill()
sys.exit(0 if ok else 1)
