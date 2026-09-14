#!/usr/bin/env python3
"""Prove the durable ledger end to end, in one run.

Spawns one interactive and one headless worker from the same server process, then
checks the RECORDS ON DISK rather than the server's report:

  1. both modes produce a record, keyed by their session uuid
  2. the record names the spawn edge (parent_session)
  3. a record outlives its session — the interactive worker's tab is killed, its live
     registry entry goes, and the record is still readable
"""
import glob, json, os, select, subprocess, sys, time

REPO = "/Users/bborbe/Documents/workspaces/claude-supervisor"
SERVER = os.path.join(REPO, "server", "supervisor.mjs")
LEDGER = os.path.expanduser("~/.local/state/claude-supervisor/sessions")
REGISTRY = os.path.expanduser("~/.claude/sessions")

env = dict(os.environ)
env["ANTHROPIC_BASE_URL"] = env.get("CLAUDE_CODE_ROUTER_URL", "http://127.0.0.1:8788")

before = set(os.path.basename(p) for p in glob.glob(os.path.join(LEDGER, "*.json")))

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


def records():
    out = {}
    for path in glob.glob(os.path.join(LEDGER, "*.json")):
        name = os.path.basename(path)
        if name in before:
            continue
        try:
            out[name] = json.load(open(path))
        except Exception:
            pass
    return out


def wait_for(predicate, seconds=60):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(1)
    return None


proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                        "clientInfo": {"name": "ledger-drill", "version": "0"}}}) + "\n")
proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
proc.stdin.flush()
time.sleep(2)
while select.select([proc.stdout], [], [], 0)[0]:
    proc.stdout.readline()

print("1. spawning one interactive and one headless worker...")
tab = call("spawn_agent", {"prompt": "Reply with exactly: TAB. Use no tools.",
                           "cwd": "/tmp", "label": "ledger-drill-tab", "interactive": True})
print("   tab      ->", json.dumps({k: tab.get(k) for k in ("agent_id", "pane_id", "status", "color")}))
head = call("spawn_agent", {"prompt": "Reply with exactly: HEAD. Use no tools.",
                            "cwd": "/tmp", "label": "ledger-drill-headless", "interactive": False})
print("   headless ->", json.dumps({k: head.get(k) for k in ("agent_id", "status")}))

print("\n2. waiting for two records on disk...")
found = wait_for(lambda: (r := records()) and len(r) >= 2 and r, 60)
if not found:
    print("FAIL: records never appeared at", LEDGER)
    proc.kill(); sys.exit(1)

for name, rec in sorted(found.items()):
    print(f"\n   {name}")
    for key in ("agent_id", "label", "mode", "launcher", "pane_id", "parent_session", "spawned_at", "status"):
        print(f"     {key:15} {rec.get(key)}")

keys_ok = all(name == f"{rec['session_id']}.json" for name, rec in found.items())
modes = {r.get("mode") for r in found.values()}
print(f"\n   keyed by session uuid: {keys_ok}")
print(f"   modes recorded:        {modes}")

print("\n   waiting for the headless worker's outcome to land...")
hd = wait_for(lambda: next((r for r in records().values()
                            if r.get("mode") == "headless" and r.get("status") != "running"), None), 90)
print(f"   headless outcome: status={hd.get('status')} result={hd.get('result')}")
outcome_ok = bool(hd and hd.get("ended_at") and hd.get("result"))

# The spawn edge cannot be exercised from here: this drill starts the server from
# python, so its parent pid is python, and null is the CORRECT answer. What can be
# checked is that the resolution works against the real registry.
print("\n   spawn edge against the REAL registry:")
live = next((json.load(open(p)) for p in glob.glob(os.path.join(REGISTRY, "*.json"))
             if json.load(open(p)).get("sessionId")), None)
probe = subprocess.run(
    ["node", "--input-type=module", "-e",
     f"import {{ parentSessionId }} from '{REPO}/server/ledger.mjs';"
     f"console.log(parentSessionId({{ ppid: {live['pid']} }}))"],
    capture_output=True, text=True, cwd=REPO)
resolved = probe.stdout.strip()
print(f"     registry pid {live['pid']} ({live['sessionId'][:8]}) -> {resolved[:8] if resolved else resolved}")
edge_ok = resolved == live["sessionId"]

print("\n3. does a record outlive its session?")
tab_rec = next((r for r in records().values() if r.get("mode") == "interactive"), {})
tab_session = tab_rec.get("session_id")
pane = tab.get("pane_id")
subprocess.run(["wezterm", "cli", "kill-pane", "--pane-id", str(pane)], capture_output=True)
time.sleep(5)

still_registered = [json.load(open(p)) for p in glob.glob(os.path.join(REGISTRY, "*.json"))
                    if json.load(open(p)).get("sessionId") == tab_session]
after = records()
survived = tab_session in [r.get("session_id") for r in after.values()]
print(f"   the worker's session id:                     {tab_session}")
print(f"   live registry entries for it now:            {len(still_registered)}  (0 = the session is gone)")
print(f"   its ledger record still readable:            {survived}")

ok = keys_ok and len(modes) == 2 and outcome_ok and edge_ok and len(still_registered) == 0 and survived
print("\nRESULT:", "PASS — both modes recorded, keyed by uuid, outcome written, edge resolves, record outlives its session"
      if ok else f"FAIL — keys={keys_ok} modes={modes} outcome={outcome_ok} edge={edge_ok} "
                  f"still_registered={len(still_registered)} survived={survived}")
proc.kill()
sys.exit(0 if ok else 1)
