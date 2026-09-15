#!/usr/bin/env python3
"""Prove the approval policy is consulted, through the real MCP tool.

The claim under test is not "the rules parse" — the unit tests cover that, and they
covered it while the server still ignored the file entirely. The claim is that a policy
CHANGES what a live worker is allowed to do, and that the server policy still escalates
what it does not cover.

Which half of that is provable depends on the machine, so the drill resolves the
worker's effective permission mode first and asserts the behaviour that mode implies:

  reachable (default / plan / dontAsk / acceptEdits)
      A/B on the same prompt and the same tool, differing only in the policy: `sleep` is
      escalated to the manager under the default policy and auto-allowed under a
      per-spawn override. Both halves are required — a drill that ran only the override
      case would pass just as well against a server that ignores the argument and allows
      everything, which is the exact failure this repo already shipped once.

  unreachable (auto / bypassPermissions)
      The hook is never consulted, so NO policy can take effect and the only correct
      outcome for a named policy is a refusal that says so. Accepted-and-ignored is the
      failure this drill exists to catch, so under these modes the refusal IS the proof,
      and the A/B is reported as not exercisable rather than quietly skipped.

Evidence is the permission log (JSONL), not the server's self-report: it records what
the server decided and which rule decided it.

⚠️ Under a reachable mode the probe command must also be one the worker's OWN settings
do not already allow. A headless worker loads user/project/local settings, so a
pre-approved command never reaches the hook at all and the drill fails for a reason that
has nothing to do with the policy. Measured 2026-09-15: `echo` was the first choice and
it proved nothing, because `Bash(echo:*)` sits in the operator's allow list. `sleep` is
not allowlisted, which is also why the refusal drill's `sleep 300` parks as intended.
"""
import json, os, select, subprocess, sys, tempfile, time

# The repo root, derived from this file's own location. A hardcoded home path works on
# exactly one machine and breaks silently everywhere else.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(REPO, "server", "supervisor.mjs")

WORK = tempfile.mkdtemp(prefix="supervisor-policy-drill-")
LOG = os.path.join(WORK, "permissions.jsonl")
OVERRIDE = os.path.join(WORK, "override.json")
MISSING = os.path.join(WORK, "does-not-exist.json")

PROMPT = "Run the shell command `sleep 1` and then reply DONE."

# Modes under which the PermissionRequest hook is never consulted. Kept in step with
# POLICY_UNREACHABLE_MODES in server/supervisor.mjs — the drill asserting a different set
# than the server refuses on would be worse than not checking.
UNREACHABLE = ("auto", "bypassPermissions")

# Allow exactly the command the worker is about to run, and nothing else. Deliberately
# narrow: a `{"tool":"Bash","match":"*","action":"allow"}` would also pass, but it would
# not distinguish "the override was read" from "the override was read and its match
# logic is broken".
with open(OVERRIDE, "w") as f:
    json.dump({"version": 1, "rules": [{"tool": "Bash", "match": "sleep", "action": "allow"}]}, f, indent=2)


def effective_mode():
    """The mode the workers will run under, resolved the same way the server resolves it.

    Asking the SDK rather than reading a settings file: the effective mode is the
    winner across every tier, and a hand-rolled read would disagree with the server
    exactly when it matters.
    """
    script = (
        "import { resolveSettings, filterEscalatingDefaultMode } from '@anthropic-ai/claude-agent-sdk';"
        f"const r = await resolveSettings({{ cwd: {json.dumps(WORK)}, settingSources: ['user','project','local'] }});"
        "const fromSettings = filterEscalatingDefaultMode(r).permissions?.defaultMode ?? null;"
        # Mirrors effectivePermissionMode in server/supervisor.mjs: an escalating settings
        # mode beats the query option, and otherwise the option governs. Resolving only the
        # settings half would disagree with the server whenever SUPERVISOR_PERMISSION_MODE
        # is set — and disagreeing with the thing under test is worse than not checking.
        f"const option = {json.dumps(os.environ.get('SUPERVISOR_PERMISSION_MODE') or 'default')};"
        "console.log(['auto','bypassPermissions'].includes(fromSettings) ? fromSettings : option)"
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, cwd=os.path.join(REPO, "server"),
    )
    return out.stdout.strip() or (out.stderr.strip().splitlines() or [""])[-1]


env = dict(os.environ)
# Route the worker through claude-code-router; never reach a provider directly.
env["ANTHROPIC_BASE_URL"] = env.get("CLAUDE_CODE_ROUTER_URL", "http://127.0.0.1:8788")
# Read at module load, so it must be set before the server starts.
env["SUPERVISOR_PERMISSION_LOG"] = LOG

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


def bash_decisions():
    """Every recorded decision for the Bash tool, oldest first."""
    try:
        with open(LOG) as f:
            rows = [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []
    return [r for r in rows if r.get("tool") == "Bash"]


def wait_for(pred, seconds=180):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(1)
    return None


def spawn(label, **extra):
    res = call("spawn_agent", {"prompt": PROMPT, "cwd": WORK, "label": label, "interactive": False, **extra})
    print("   ->", json.dumps(res))
    return res


send("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "policy-drill", "version": "0"}})
proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
proc.stdin.flush()
time.sleep(2)  # let initialize settle; drain anything buffered
while select.select([proc.stdout], [], [], 0)[0]:
    proc.stdout.readline()

failures = []

print("0. resolving the effective permission mode the workers will run under...")
mode = effective_mode()
unreachable = mode in UNREACHABLE
print(f"   -> {mode!r} ({'unreachable — the hook is never consulted' if unreachable else 'reachable'})")
if not mode:
    failures.append("could not resolve the effective permission mode — the drill cannot say which behaviour to expect")

if unreachable:
    # ── the hook cannot be consulted, so a named policy must be refused ──────
    print("1. a named policy under a mode that bypasses the hook...")
    refused = spawn("policy-drill-unreachable", policy=OVERRIDE)
    if refused.get("agent_id"):
        failures.append(
            "a policy was accepted under a mode that never consults the hook — it would be reported as "
            "applied and silently ignored, which is the failure this drill exists to catch"
        )
    elif mode not in refused.get("error", ""):
        failures.append(f"refused, but the error does not name the mode that caused it: {refused.get('error')!r}")
    else:
        print("   OK — refused, and the error names the mode")

    print("2. the same spawn WITHOUT a policy (the documented escape)...")
    plain = spawn("policy-drill-plain")
    if not plain.get("agent_id"):
        failures.append(f"the escape hatch does not work — a worker cannot be spawned at all: {plain.get('error')!r}")
    else:
        print("   OK — spawns normally when no policy is named")

    print()
    print(f"NOTE: the A/B is not exercisable here. mode={mode!r} bypasses the hook AND canUseTool,")
    print("      so no policy can change a live worker's behaviour on this machine. The refusal")
    print("      above is the whole provable claim under this mode.")
else:
    # ── 1. a named policy that cannot be read must refuse, not fall back ─────
    print("1. a policy path that does not exist...")
    refused = spawn("policy-drill-missing", policy=MISSING)
    if refused.get("agent_id"):
        failures.append("a missing policy file was accepted — the worker would run under the default while the caller believes otherwise")
    elif MISSING not in refused.get("error", ""):
        failures.append(f"refused, but the error does not name the path that failed: {refused.get('error')!r}")
    else:
        print("   OK — refused, and the error names the path")

    # ── 2. the default policy escalates unknown Bash to the manager ──────────
    print("2. default policy: is `sleep` escalated to the manager?")
    before = len(bash_decisions())
    spawned = spawn("policy-drill-default")
    agent = spawned.get("agent_id")
    if not agent:
        failures.append("no agent spawned for the default-policy case")
    else:
        record = wait_for(lambda: (bash_decisions()[before:] or [None])[0])
        if not record:
            failures.append(
                "no Bash decision was logged — the request never reached the server. Check that the probe "
                "command is not already allowed by the worker's own settings."
            )
        else:
            print("   -> logged:", json.dumps(record))
            if record.get("decided_by") != "escalated":
                failures.append(f"default policy did not escalate: decided_by={record.get('decided_by')!r}")
            elif record.get("decision") is not None:
                failures.append(f"an escalated request carried a decision: {record.get('decision')!r}")
            else:
                parked = wait_for(lambda: [p for p in call("pending_permissions", {}) if p.get("agent_id") == agent] or None, 60)
                if not parked:
                    failures.append("escalated in the log but never parked for the manager — the fall-through did not reach canUseTool")
                else:
                    print("   -> parked for the manager:", parked[0]["request_id"])
                    call("answer_permission", {"request_id": parked[0]["request_id"], "behavior": "allow"})

    # ── 3. the per-spawn override is consulted, without the manager ──────────
    print("3. per-spawn policy: is `sleep` auto-allowed?")
    before = len(bash_decisions())
    spawned = spawn("policy-drill-override", policy=OVERRIDE)
    agent = spawned.get("agent_id")
    if not agent:
        failures.append("no agent spawned for the override case")
    elif spawned.get("policy") != OVERRIDE:
        failures.append(f"the spawn did not report the policy it applied: {spawned.get('policy')!r}")
    else:
        record = wait_for(lambda: (bash_decisions()[before:] or [None])[0])
        if not record:
            failures.append("no Bash decision was logged for the override case")
        else:
            print("   -> logged:", json.dumps(record))
            if record.get("decided_by") != "policy" or record.get("decision") != "allow":
                failures.append(f"the override was not consulted: decided_by={record.get('decided_by')!r} decision={record.get('decision')!r}")
            elif record.get("policy") != OVERRIDE:
                failures.append(f"the decision does not name the policy that made it: {record.get('policy')!r}")
            else:
                still_parked = [p for p in call("pending_permissions", {}) if p.get("agent_id") == agent]
                if still_parked:
                    failures.append("auto-allowed in the log but still parked for the manager — the two disagree")
                else:
                    print("   OK — allowed by policy, never reached the manager")

print()
proc.kill()
if failures:
    for f in failures:
        print("FAIL:", f)
    sys.exit(1)
print("RESULT: PASS")
print("        permission log:", LOG)
sys.exit(0)
