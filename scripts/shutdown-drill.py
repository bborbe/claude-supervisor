#!/usr/bin/env python3
"""Prove the shutdown path end to end, against a throwaway server only.

Kills a server THIS SCRIPT started, then checks the RECORDS ON DISK rather than the
server's self-report:

  1. both spawned workers have a record, and the headless one is genuinely MID-TURN
  2. after SIGTERM, no record that server wrote still says `running`
  3. each reads `unknown` + `supervisor_exited_at` + a per-kind reason
  4. nothing is marked `done`/`error` — no outcome was observed, so none may be claimed
  5. the server actually exits, promptly — a shutdown path that waits is a hang

Step 1 is the load-bearing one. A worker that had already finished before the signal
would be legitimately `done`, so stamping it `unknown` would be the *wrong* answer and
the drill would prove nothing about the shutdown path. The headless worker is therefore
told to sleep, and the drill refuses to draw a conclusion unless its record is still
`running` when the signal is sent.

SAFETY: the signal goes to this script's own child by pid, never to a live supervisor.
The ledger directory is SHARED with the live fleet, so every assertion is scoped to
records absent from the pre-run listing — pre-existing records are never read as
evidence and never written.
"""
import glob, json, os, select, signal, subprocess, sys, time

# The repo root, derived from this file's own location. A hardcoded home path works on
# exactly one machine and breaks silently everywhere else.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(REPO, "server", "supervisor.mjs")
LEDGER = os.path.expanduser("~/.local/state/claude-supervisor/sessions")

env = dict(os.environ)
env["ANTHROPIC_BASE_URL"] = env.get("CLAUDE_CODE_ROUTER_URL", "http://127.0.0.1:8788")

before = set(os.path.basename(p) for p in glob.glob(os.path.join(LEDGER, "*.json")))

# The labels that identify this drill's own records, so a sibling session's concurrent
# spawn cannot be mistaken for one of ours. See records().
TAB_LABEL = "shutdown-drill-tab"
HEAD_LABEL = "shutdown-drill-headless"

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
    """Only the records THIS run created.

    Filtered by label, NOT merely by "absent from the pre-run listing". The ledger
    directory is shared with the live fleet and sibling sessions spawn workers
    constantly — four appeared inside a seven-minute window while this drill was being
    written. Those records belong to OTHER, still-live servers and are legitimately
    `running`; asserting `unknown` over them would fail the drill for a reason that has
    nothing to do with the shutdown path. Snapshot-diffing alone is not enough.
    """
    out = {}
    for path in glob.glob(os.path.join(LEDGER, "*.json")):
        name = os.path.basename(path)
        if name in before:
            continue
        try:
            rec = json.load(open(path))
        except Exception:
            continue
        if rec.get("label") in (TAB_LABEL, HEAD_LABEL):
            out[name] = rec
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
                                        "clientInfo": {"name": "shutdown-drill", "version": "0"}}}) + "\n")
proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
proc.stdin.flush()
time.sleep(2)
while select.select([proc.stdout], [], [], 0)[0]:
    proc.stdout.readline()

print("1. spawning one interactive and one headless worker into a THROWAWAY server...")
tab = call("spawn_agent", {"prompt": "Reply with exactly: TAB. Use no tools.",
                           "cwd": "/tmp", "label": TAB_LABEL, "interactive": True})
print("   tab      ->", json.dumps({k: tab.get(k) for k in ("agent_id", "pane_id", "status")}))
# Sleeps, so it is still mid-turn when the signal arrives. Without this the worker could
# finish first and be legitimately `done`, which would make the whole drill vacuous.
head = call("spawn_agent", {"prompt": "Use the Bash tool to run exactly: sleep 300. Then reply DONE.",
                            "cwd": "/tmp", "label": HEAD_LABEL, "interactive": False})
print("   headless ->", json.dumps({k: head.get(k) for k in ("agent_id", "status")}))

print("\n2. waiting for two records on disk...")
found = wait_for(lambda: (r := records()) and len(r) >= 2 and r, 60)
if not found:
    print("FAIL: records never appeared at", LEDGER)
    proc.kill()
    sys.exit(1)

for name, rec in sorted(found.items()):
    print(f"   {name[:20]}...  mode={rec.get('mode'):11} status={rec.get('status')}")

headless_rec = next((r for r in found.values() if r.get("mode") == "headless"), None)
if not headless_rec:
    print("FAIL: no headless record — the drill cannot test the mid-turn case")
    proc.kill()
    sys.exit(1)

# The precondition that gives the drill its meaning. A headless worker that already
# finished is legitimately terminal; asserting `unknown` over it would be a false pass.
if headless_rec.get("status") != "running":
    print(f"\nFAIL: the headless worker is already {headless_rec.get('status')!r} — it was meant to be")
    print("      mid-turn at signal time, so this run proves nothing about the shutdown path.")
    proc.kill()
    sys.exit(1)
print("   headless is mid-turn (status=running) — the stamp has something to correct")

print("\n3. sending SIGTERM to the throwaway server (pid %d)..." % proc.pid)
sent = time.time()
proc.send_signal(signal.SIGTERM)
exited = True
try:
    code = proc.wait(timeout=15)
    elapsed = time.time() - sent
    print(f"   exited with {code} after {elapsed:.2f}s")
except subprocess.TimeoutExpired:
    elapsed = time.time() - sent
    print(f"   STILL RUNNING after {elapsed:.2f}s — a shutdown path that waits is a hang")
    exited = False
    proc.kill()

after = records()
print("\n4. reading every record that server wrote...")
still_running, stamped, claimed_outcome, thin = [], [], [], []
for name, rec in sorted(after.items()):
    status = rec.get("status")
    print(f"   {name[:20]}...  mode={rec.get('mode'):11} status={status}")
    if status == "running":
        still_running.append(name)
    if status == "unknown":
        stamped.append(name)
        if not rec.get("supervisor_exited_at"):
            thin.append(f"{name}: no supervisor_exited_at")
        if not rec.get("unknown_reason"):
            thin.append(f"{name}: no unknown_reason")
    if status in ("done", "error"):
        claimed_outcome.append(name)

print("\n5. verdict...")
checks = {
    "no record still says running": not still_running,
    "both records stamped unknown": len(stamped) == len(after),
    "every stamp carries time + reason": not thin,
    "no outcome claimed for an unobserved worker": not claimed_outcome,
    "the server exited promptly": exited,
}
for label, ok in checks.items():
    print(f"   {'PASS' if ok else 'FAIL'}  {label}")

if still_running:
    print(f"   still running:  {still_running}")
if claimed_outcome:
    print(f"   claimed done/error: {claimed_outcome}")
if thin:
    print(f"   incomplete stamps: {thin}")

ok = all(checks.values())
print("\nRESULT:", "PASS — every worker the server owned is stamped unknown, and nothing is claimed that was not observed"
      if ok else "FAIL — see the per-check lines above")

# Tidy up the tab this drill created. It is ours, spawned a moment ago; the live fleet's
# tabs are never touched.
pane = tab.get("pane_id")
if pane:
    subprocess.run(["wezterm", "cli", "kill-pane", "--pane-id", str(pane)], capture_output=True)

sys.exit(0 if ok else 1)
