#!/usr/bin/env python3
"""Prove the durable ledger end to end, in one run.

Spawns one interactive and one headless worker from the same server process, then
checks the RECORDS ON DISK rather than the server's report:

  1. both modes produce a record, keyed by their session uuid
  2. the record names the spawn edge (parent_session)
  3. a record outlives its session — the interactive worker's tab is killed, its live
     registry entry goes, and the record is still readable
"""
import glob, importlib.util, json, os, select, subprocess, sys, time

# The repo root, derived from this file's own location. A hardcoded home path works on
# exactly one machine and breaks silently everywhere else.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(REPO, "server", "supervisor.mjs")
REGISTRY = os.path.expanduser("~/.claude/sessions")

_SPAWN_LEDGER = None


def _spawn_ledger():
    """Import `spawn-ledger.py` (hyphenated filename -> importlib) — the ONE ledger reader.

    ⚠️ **The ledger moved to the attention store's endpoint on 2026-10-14.** This drill used to
    watch `<session_id>.json` files appear in a directory; it watches the store's rows appear
    now, which is the same claim about the same durable record. ⚠️ The endpoint is what every
    reader consults, so a drill still watching the file would prove a store nothing reads.
    """
    global _SPAWN_LEDGER
    if _SPAWN_LEDGER is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spawn-ledger.py")
        spec = importlib.util.spec_from_file_location("spawn_ledger", path)
        _SPAWN_LEDGER = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_SPAWN_LEDGER)
    return _SPAWN_LEDGER


env = dict(os.environ)
env["ANTHROPIC_BASE_URL"] = env.get("CLAUDE_CODE_ROUTER_URL", "http://127.0.0.1:8788")

# ⚠️ **An unreadable store at the START is FATAL, not an empty `before`.** With an empty set
# every pre-existing row would read as new, and the drill would "pass" against a store it
# never read — the false-clean shape this drill exists to rule out.
before = _spawn_ledger().read_ledger()
if before is None:
    print("FAIL: cannot read the session-ledger endpoint at", _spawn_ledger().endpoint_base())
    sys.exit(1)
before = set(before)

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
    """Rows this run created — those whose session id was absent when the drill started.

    ⚠️ Keyed by the ROW'S OWN `session_id`, which is also the only sound join key: `agent_id`
    is a per-process counter that restarts at 0 on every reconnect.
    """
    rows = _spawn_ledger().read_ledger() or {}
    return {sid: row for sid, row in rows.items() if sid not in before}


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
    print("FAIL: rows never appeared at", _spawn_ledger().endpoint_base())
    proc.kill(); sys.exit(1)

for sid, rec in sorted(found.items()):
    print(f"\n   {sid}")
    for key in ("agent_id", "label", "mode", "launcher", "pane_id", "spawner_session_id",
                "spawned_at", "status"):
        print(f"     {key:19} {rec.get(key)}")

# ⚠️ The claim is unchanged, and it is still a real one: the map's key must BE the row's own
# `session_id`. It used to compare a FILENAME against the row; it compares the store's key
# against the row now — the same identity claim, and the one `agent_id` cannot make.
keys_ok = all(sid == rec.get("session_id") for sid, rec in found.items())
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
# ⚠️ `after` is keyed by the row's own session id, so membership IS the claim — the list
# comprehension this replaces asked the same question through a second shape.
survived = tab_session in after
print(f"   the worker's session id:                     {tab_session}")
print(f"   live registry entries for it now:            {len(still_registered)}  (0 = the session is gone)")
print(f"   its ledger record still readable:            {survived}")

ok = keys_ok and len(modes) == 2 and outcome_ok and edge_ok and len(still_registered) == 0 and survived
print("\nRESULT:", "PASS — both modes recorded, keyed by uuid, outcome written, edge resolves, record outlives its session"
      if ok else f"FAIL — keys={keys_ok} modes={modes} outcome={outcome_ok} edge={edge_ok} "
                  f"still_registered={len(still_registered)} survived={survived}")
proc.kill()
sys.exit(0 if ok else 1)
