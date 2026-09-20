#!/usr/bin/env python3
"""Prove `agent_status.current_tool_call` answers "which call, and for how long" on live
headless workers — in the healthy case AND the parked one.

Why both cases, and why this drill exists at all: `agent_status` reports neither of them as
idle (`running`/`busy` and `blocked-on-permission`/`waiting` respectively), so until this field
the two were indistinguishable from outside, and only one of them needs a human. The field has
to name the call in BOTH — the transcript carries a pending `tool_use` either way — while the
registry remains the thing that says which state the worker is in.

Four things are asserted, none of which a unit test can see:

  1. A running headless worker inside a long call reports that call by name.
  2. A worker parked on a permission gate reports its call too — `Bash` with the command it is
     waiting to run — with `session_status`/`awaiting_input`, not this field, saying it is
     parked.
  3. `held_seconds` GROWS between two samples taken seconds apart, i.e. it is measured from the
     transcript's own timestamp and does not reset when a caller looks again. A field that
     reset on every poll would be useless to a manager polling every few minutes.
  4. A worker with nothing in flight reports `null` — the field does not invent a call.

Safe against a live fleet: it spawns its own three workers and leaves the parked one's gate
unanswered to auto-deny. None is a mock, and none is adopted from anywhere.

Two fixture choices are measured, not guessed — both cost a failed run to learn:

  - **The healthy call is an `Agent` dispatch, not a shell command.** Every `sleep` form is
    refused in milliseconds by the harness's own Bash guard ("Blocked: standalone sleep 75…",
    and "Blocked: sleep 60 followed by: echo done" for the compound), so a sleeping fixture is
    never in flight at all; the worker's fallback is `run_in_background`, which returns at once.
    A subagent dispatch blocks for as long as the subagent runs and needs no permission.
  - **The parked call is `ls /tmp` from a cwd that does not contain `/tmp`.** A worker spawned
    with `cwd: "/tmp"` finds `/tmp` INSIDE its own cwd, the call is allowed, and the run
    produces two workers and zero parked ones. The first version of this drill did exactly
    that.
"""
import json, os, select, subprocess, sys, time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(REPO, "server", "supervisor.mjs")

HEALTHY_PROMPT = ("Use the Agent tool to launch one subagent — subagent_type 'general-purpose', "
                  "prompt 'Reply with exactly OK and use no tools.' Wait for it to return, then "
                  "report its reply.")
PARKED_PROMPT = ("Use the Bash tool to run exactly this command, and nothing else first: ls /tmp")
IDLE_PROMPT = "Reply with exactly OK and use no tools at all."

env = dict(os.environ)
env["ANTHROPIC_BASE_URL"] = env.get("CLAUDE_CODE_ROUTER_URL", "http://127.0.0.1:8788")

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


def spawn(prompt, label):
    r = call("spawn_agent", {"prompt": prompt, "cwd": REPO, "label": label, "interactive": False})
    if "agent_id" not in r:
        print(f"   -> spawn of {label} failed: {json.dumps(r)[:300]}")
        proc.kill()
        sys.exit(1)
    return r["agent_id"]


def wait_for(agent_id, predicate, timeout=90):
    """The first status sample satisfying `predicate`, or the last one seen on timeout."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = call("agent_status", {"agent_id": agent_id})
        if predicate(last):
            return last
        time.sleep(2)
    return last


def show(tag, st):
    print(f"   {tag}: status={st.get('status')!r} session_status={st.get('session_status')!r} "
          f"awaiting_input={st.get('awaiting_input')!r} pending={st.get('pending_permissions')}")
    print(f"      current_tool_call={json.dumps(st.get('current_tool_call'))}")


def mid_call_while_running(st):
    return bool(st.get("current_tool_call")) and st.get("awaiting_input") is False


def parked_on_a_gate(st):
    """Inside a call AND the registry says blocked on input. Both halves matter: the transcript
    cannot tell the two states apart, so waiting on `current_tool_call` alone would sample a
    worker that is merely running."""
    return bool(st.get("current_tool_call")) and (st.get("awaiting_input") is True
                                                 or bool(st.get("pending_permissions")))


proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                        "clientInfo": {"name": "current-call-drill", "version": "0"}}}) + "\n")
proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
proc.stdin.flush()
time.sleep(2)
while select.select([proc.stdout], [], [], 0)[0]:
    proc.stdout.readline()

print("1. healthy headless worker, inside a subagent dispatch:")
healthy = spawn(HEALTHY_PROMPT, "current-call-drill-healthy")
h1 = wait_for(healthy, mid_call_while_running)
show("sample", h1)

print("\n2. parked headless worker, `ls /tmp` waiting on a permission gate:")
parked = spawn(PARKED_PROMPT, "current-call-drill-parked")
p1 = wait_for(parked, parked_on_a_gate)
show("first sample ", p1)
time.sleep(10)
p2 = call("agent_status", {"agent_id": parked})
show("second sample", p2)

print("\n3. idle headless worker, nothing in flight:")
idle = spawn(IDLE_PROMPT, "current-call-drill-idle")
i1 = wait_for(idle, lambda st: st.get("status") == "done")
show("after the turn", i1)

ch, cp1, cp2, ci = (h1.get("current_tool_call") or {}, p1.get("current_tool_call") or {},
                    p2.get("current_tool_call") or {}, i1.get("current_tool_call") or {})
healthy_named = bool(ch.get("name")) and bool(ch.get("input_summary")) and h1.get("awaiting_input") is False
parked_named = cp1.get("name") == "Bash" and "ls /tmp" in (cp1.get("input_summary") or "")
parked_is_parked = p1.get("session_status") == "waiting" or p1.get("awaiting_input") is True
grew = (cp1.get("held_seconds") is not None and cp2.get("held_seconds") is not None
        and cp2["held_seconds"] - cp1["held_seconds"] >= 5)
idle_empty = i1.get("current_tool_call") is None

print(f"\n   healthy: {ch.get('name')!r} {str(ch.get('input_summary'))[:40]!r} while running={healthy_named}")
print(f"   parked : {cp1.get('name')!r} {cp1.get('input_summary')!r} parked={parked_is_parked} named={parked_named}")
print(f"   held   : {cp1.get('held_seconds')} -> {cp2.get('held_seconds')} s, grew={grew}")
print(f"   idle   : current_tool_call is null={idle_empty}")
ok = healthy_named and parked_named and parked_is_parked and grew and idle_empty
print("RESULT:", "PASS — both states report the call and its duration, and an idle worker reports none"
      if ok else "FAIL — see the samples above")
proc.kill()
sys.exit(0 if ok else 1)
