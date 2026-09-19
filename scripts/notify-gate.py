#!/usr/bin/env python3
"""Publish manager ACTION gates to the notification core, with cadence de-dup.

stdin: JSON {"gates": [{"owner": "<session id or pane id>", "text": "<gate line>"}]}
       An empty list is a clean sweep -- it prunes and sends nothing.

Why this exists: a manager that raises an ACTION gate reaches only an operator who
is at the machine or listening, because TTS cannot leave the room. This publishes
the same gate to Telegram, so a gate raised while the operator is away arrives.

Two de-dup axes, and this script owns only the second:

  * WHICH manager notifies is already decided upstream -- `commands/fleet-manager.md`
    drops an entry whose owning worker manager is live on the roster, so any given
    gate reaches this script at most once per sweep.
  * HOW OFTEN is this script's ledger -- and it is PER LAYER, because each layer's
    sweep is a partial view (the fleet drops worker-owned gates). A shared ledger
    would let one layer prune the other's gates as "cleared", after which they
    would re-raise forever. The manager loop re-invokes every ~15 min (fleet) and
    ~5 min (worker), so an unbounded re-raise would put one open gate on the phone
    roughly 96 times a day -- the noise the TTS gate exists to prevent, moved to a
    louder channel.

Cadence, decided 2026-09-19: once on raise, then re-raise at 1h and 4h while the
gate is still open, capped at 3 deliveries per gate identity. After the third,
silence until the gate changes or clears.

Gate identity is (owner, normalised text). Normalising collapses whitespace so a
re-rendered line is not mistaken for a new gate; a genuinely different text IS a
new identity, because the question changed and the operator should hear it.

Pruning is what makes the bound per-gate rather than per-lifetime: an identity
absent from a sweep is a gate that cleared, so it is dropped and the SAME gate can
raise again later at full cadence.

Config: ~/.config/claude-supervisor/config.json (override SUPERVISOR_CONFIG):
  {"notify": {"env": "dev",
              "endpoints": {"dev":  {"baseUrl": "...", "teamvaultKey": "..."},
                            "prod": {"baseUrl": "...", "teamvaultKey": "..."}}}}
Absent or incomplete FAILS LOUDLY, and only when a gate is actually due. A silent
skip is the worst failure mode here: the gate is real, the operator is needed, and
nothing arrives -- and it reads exactly like a clean sweep.

Ledger: ~/.claude/state/gate-notifications-<layer>.json, one per `--layer`
(override SUPERVISOR_GATE_STATE, which is also what lets the tests exercise
commit/prune without touching the operator's real ledger). Per layer, not shared
-- see ledger_path() for why a shared file silently defeats the cadence.
"""
import argparse
import datetime
import fcntl
import json
import os
import subprocess
import sys

CONFIG_PATH = os.environ.get("SUPERVISOR_CONFIG") or os.path.expanduser(
    "~/.config/claude-supervisor/config.json"
)
STATE_PATH = None  # set by main() from --layer; see ledger_path()


def ledger_path(layer):
    """The ledger is PER LAYER, and that is load-bearing rather than tidy.

    The two manager layers see different slices of the world: `fleet-manager` drops
    every gate whose owning worker manager is live, so its sweep is deliberately a
    subset. A shared ledger would let the fleet's sweep prune a worker-owned gate
    (absent from *its* list), after which the worker's next sweep sees that gate as
    first-sight and republishes it with a fresh count -- so a gate that stays open
    would re-notify on every fleet tick, forever, instead of at most MAX_DELIVERIES
    times. That is precisely the noise this script exists to prevent.

    Scoping the ledger to the layer makes "absent from this sweep" mean "cleared
    *for this layer*", which is the only reading that is true for a partial view.
    """
    override = os.environ.get("SUPERVISOR_GATE_STATE")
    if override:
        return override
    return os.path.expanduser(f"~/.claude/state/gate-notifications-{layer}.json")

# Mirrors the vault script this replaces: pinned deliberately rather than
# ${TEAMVAULT_CONFIG:-...}, because that variable is commonly exported to a
# different instance in an interactive shell and `:-` lets the export win.
TEAMVAULT_CONFIG = os.environ.get("SUPERVISOR_TEAMVAULT_CONFIG") or os.path.expanduser(
    "~/.config/teamvault-cli/config.json"
)

# The cadence is a decided contract, not a per-install knob -- so it is a constant
# and not config. Re-raising at 1h and 4h after the FIRST raise, not after the last.
RE_RAISE_AFTER_SECONDS = (3600, 14400)
MAX_DELIVERIES = 3

PUBLISH_PATH = "/api/1.0/command/core-notification-v1/notification-publish?sync=true"

# `pending-approval` routes to Telegram on both stages and is the type the bot-split
# classifies as "needing a human decision" -- which is what a manager gate is.
# `agent-escalation` is deliberately NOT the default: it is classified as machine
# noise, so it would move to a muted bot the day that split is revived.
DEFAULT_TYPE = "pending-approval"


def normalise(text):
    """Collapse whitespace so a re-rendered gate keeps one identity."""
    return " ".join(str(text).split())


def gate_key(owner, text):
    return f"{normalise(owner)}\x1f{normalise(text)}"


def parse_time(value):
    return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc
    )


def format_time(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def decide(entry, now):
    """True when this gate is due a delivery. `entry` is None on first sight."""
    if entry is None:
        return True
    if entry["deliveries"] >= MAX_DELIVERIES:
        return False
    elapsed = (now - parse_time(entry["firstRaisedAt"])).total_seconds()
    return elapsed >= RE_RAISE_AFTER_SECONDS[entry["deliveries"] - 1]


def render_message(gate):
    """Name the gate and its owner. Never present itself as answerable."""
    return (
        f"Manager gate open -- {normalise(gate['owner'])}: {normalise(gate['text'])}\n"
        "Notification only. Answer it in the owning session, not here."
    )


def load_config():
    try:
        with open(CONFIG_PATH) as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}
    except ValueError as error:
        sys.exit(f"notify-gate: {CONFIG_PATH} is not valid JSON: {error}")
    except OSError as error:
        # An unreadable config is a real failure the manager must see as a fix, not
        # as a traceback -- FileNotFoundError is handled above, so this is the
        # permission/IO class the docstring's "fails loudly" promise also covers.
        sys.exit(f"notify-gate: cannot read {CONFIG_PATH}: {error}")


def resolve_endpoint(config):
    notify = config.get("notify")
    if not notify:
        sys.exit(
            f"notify-gate: no `notify` block in {CONFIG_PATH} and a gate is due -- "
            "refusing to skip silently. Add "
            '{"notify":{"env":"dev","endpoints":{"dev":{"baseUrl":"...",'
            '"teamvaultKey":"..."}}}} and retry.'
        )
    if not isinstance(notify, dict):
        sys.exit(
            f"notify-gate: `notify` in {CONFIG_PATH} must be an object, got "
            f"{type(notify).__name__} -- the shape is in the README."
        )
    endpoints = notify.get("endpoints")
    if not isinstance(endpoints, dict):
        sys.exit(
            f"notify-gate: `notify.endpoints` in {CONFIG_PATH} must be an object "
            f"keyed by env, got {type(endpoints).__name__} -- the shape is in the README."
        )
    env = notify.get("env", "dev")
    endpoint = endpoints.get(env) or {}
    if (
        not isinstance(endpoint, dict)
        or not endpoint.get("baseUrl")
        or not endpoint.get("teamvaultKey")
    ):
        sys.exit(
            f"notify-gate: config has no complete endpoint for env `{env}` in "
            f"{CONFIG_PATH} -- need both baseUrl and teamvaultKey."
        )
    # `or` rather than a get() default: a key present but null must still fall back,
    # or the core receives {"type": null} and rejects the publish.
    return env, endpoint, notify.get("type") or DEFAULT_TYPE


def teamvault_field(field, key):
    result = subprocess.run(
        [
            "teamvault-cli",
            field,
            "--teamvault-config",
            TEAMVAULT_CONFIG,
            f"--teamvault-key={key}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.exit(
            f"notify-gate: teamvault-cli {field} failed for key {key}: "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def curl_config_user(username, password):
    """A curl `-K` fragment. Escaping is required: a raw `"` would end the string."""
    value = (
        f"{username}:{password}"
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        # A raw newline would end the `user =` line early, so the remainder would be
        # parsed as further curl directives rather than as part of the credential.
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'user = "{value}"\n'


def publish(base_url, teamvault_key, message, notification_type):
    """POST one notification. Credentials travel on stdin, never argv.

    `-u user:pass` would put the secret in this process's argument list, where any
    process the same user runs can read it back out of `ps`. `-K -` keeps it off
    argv; the body and URL stay on argv because neither is secret.
    """
    username = teamvault_field("username", teamvault_key)
    password = teamvault_field("password", teamvault_key)
    body = json.dumps({"type": notification_type, "message": message})
    result = subprocess.run(
        [
            "curl",
            "-s",
            # `-s` alone exits 0 for any completed HTTP transaction, so a 4xx/5xx
            # would read as a successful publish and be written to the ledger as
            # delivered -- and three of those silence the gate for good while every
            # run still reports success. That is the worst failure mode this script
            # has, so the fail flag is load-bearing, not decoration.
            "--fail-with-body",
            # Bounds the publish, which is what makes holding the ledger lock safe:
            # an unbounded curl would block the other manager layer indefinitely.
            "--max-time",
            "30",
            "-K",
            "-",
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json",
            "-d",
            body,
            f"{base_url}{PUBLISH_PATH}",
        ],
        input=curl_config_user(username, password),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # `--fail-with-body` still prints the response body, which is where the
        # core puts its own reason; stderr alone would carry only the status.
        detail = result.stdout.strip() or result.stderr.strip()
        sys.exit(f"notify-gate: publish failed: {detail}")
    return result.stdout.strip()


def valid_entry(entry):
    """A shape-malformed entry is dropped, not fatal.

    load_ledger()'s contract is "never fatal", and that has to hold for entries
    that parse as JSON but carry the wrong shape: a missing `deliveries` KeyErrors
    inside decide(), and a malformed `firstRaisedAt` ValueErrors inside parse_time().
    Dropping such an entry costs at most one re-notification.
    """
    if not isinstance(entry, dict):
        return False
    deliveries = entry.get("deliveries")
    # `bool` is an int subclass, so it has to be excluded explicitly; and a count
    # below 1 indexes RE_RAISE_AFTER_SECONDS out of range inside decide().
    if isinstance(deliveries, bool) or not isinstance(deliveries, int) or deliveries < 1:
        return False
    try:
        parse_time(entry["firstRaisedAt"])
    except (KeyError, TypeError, ValueError):
        return False
    return True


def load_ledger():
    """An unreadable ledger reads as empty, never fatal.

    The file is rewritten every sweep, so a write truncated by a dying process
    would otherwise make every later run die on a JSON traceback -- turning one bad
    write into the permanent loss of gate notifications. The same holds for a file
    that parses but is not a mapping, and for entries of the wrong shape.
    """
    try:
        with open(STATE_PATH) as handle:
            ledger = json.load(handle)
    except (FileNotFoundError, ValueError):
        return {}
    if not isinstance(ledger, dict):
        return {}
    return {key: entry for key, entry in ledger.items() if valid_entry(entry)}


def lock_ledger():
    """Serialise the read-modify-write *within* a layer, not across layers.

    Since the ledger is per layer the two layers hold different files and cannot
    contend at all. The lock is still load-bearing for two overlapping sweeps of
    the SAME layer -- a ScheduleWakeup round re-entering while the previous one is
    still publishing -- where the later writer would otherwise read a file that
    predates the earlier writer's commit, losing one side's delivery count and
    re-notifying that gate later. `--max-time` on the publish bounds how long this
    is held.
    """
    ensure_state_dir()
    handle = open(f"{STATE_PATH}.lock", "w")
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def ensure_state_dir():
    """Create the ledger's directory if it has one.

    A bare filename -- `SUPERVISOR_GATE_STATE=gate.json`, which the README's
    override permits -- has no directory part, and `os.makedirs("")` raises
    rather than no-opping.
    """
    directory = os.path.dirname(STATE_PATH)
    if not directory:
        return
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as error:
        sys.exit(f"notify-gate: cannot create {directory}: {error}")


def commit(ledger, current, updates):
    """Keep only identities still open, then apply this sweep's updates.

    Pruning falls out of `kept`: a gate that cleared is absent from `current`, so
    its count is dropped and a later re-raise starts fresh.

    Written through a temp file and renamed, so no reader ever sees a half-written
    ledger even if this process dies mid-dump.
    """
    kept = {key: ledger[key] for key in current if key in ledger}
    kept.update(updates)
    ensure_state_dir()
    tmp = f"{STATE_PATH}.tmp"
    with open(tmp, "w") as handle:
        json.dump(kept, handle, indent=2, sort_keys=True)
    os.replace(tmp, STATE_PATH)
    return kept


def read_gates():
    """Parse stdin, reporting malformed input in this script's own style.

    Every other failure here exits with a `notify-gate: …` line naming the fix, and
    the commands tell the manager to surface that message -- so a raw traceback
    would be surfaced *instead of* a fix, on a one-liner the manager types by hand.
    """
    try:
        payload = json.load(sys.stdin)
    except ValueError as error:
        sys.exit(f"notify-gate: stdin is not valid JSON: {error}")
    if not isinstance(payload, dict):
        sys.exit(
            f"notify-gate: stdin must be an object with a `gates` list, got "
            f"{type(payload).__name__} -- the call is "
            '\'{"gates": [{"owner": "...", "text": "..."}]}\''
        )
    if "gates" not in payload:
        sys.exit(
            "notify-gate: stdin has no `gates` key. Pass an explicit "
            '{"gates": []} for a clean sweep -- a missing key is a typo, and '
            "reading it as empty would silently prune every open gate."
        )
    gates = payload["gates"]
    if not isinstance(gates, list):
        sys.exit(f"notify-gate: `gates` must be a list, got {type(gates).__name__}")
    for gate in gates:
        if not isinstance(gate, dict):
            sys.exit(
                f"notify-gate: each gate must be an object, got "
                f"{type(gate).__name__}: {json.dumps(gate)}"
            )
        for field in ("owner", "text"):
            if not gate.get(field):
                sys.exit(
                    f"notify-gate: a gate is missing `{field}`: {json.dumps(gate)} "
                    '-- each gate needs {"owner": "...", "text": "..."}'
                )
    return gates


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Publish a manager sweep's ACTION gates to the notification core."
    )
    parser.add_argument(
        "--layer",
        required=True,
        help=(
            "which manager layer this sweep belongs to (fleet, worker, ...). "
            "Namespaces the cadence ledger: the layers see different slices of the "
            "world, so a shared ledger would have one prune the other's gates and "
            "re-notify them forever."
        ),
    )
    return parser.parse_args(argv)


def main():
    global STATE_PATH
    args = parse_args(sys.argv[1:])
    STATE_PATH = ledger_path(args.layer)

    now = datetime.datetime.now(datetime.timezone.utc)
    gates = read_gates()
    # Held for the whole round and released when the file closes, so the read, the
    # publishes and the commit form one critical section rather than three.
    with lock_ledger():
        publish_round(now, gates)


def publish_round(now, gates):
    ledger = load_ledger()
    current = {gate_key(g["owner"], g["text"]): g for g in gates}
    due = [
        (key, gate) for key, gate in current.items() if decide(ledger.get(key), now)
    ]

    if not due:
        commit(ledger, current, {})
        print(f"notify-gate: {len(current)} open, 0 due")
        return

    config = load_config()
    env, endpoint, notification_type = resolve_endpoint(config)

    updates = {}
    delivered = []
    try:
        for key, gate in due:
            entry = ledger.get(key)
            publish(
                endpoint["baseUrl"],
                endpoint["teamvaultKey"],
                render_message(gate),
                notification_type,
            )
            deliveries = 1 if entry is None else entry["deliveries"] + 1
            updates[key] = {
                "firstRaisedAt": entry["firstRaisedAt"] if entry else format_time(now),
                "lastDeliveryAt": format_time(now),
                "deliveries": deliveries,
                "owner": normalise(gate["owner"]),
                "text": normalise(gate["text"]),
            }
            delivered.append(f"{gate['owner']} ({deliveries}/{MAX_DELIVERIES})")
    finally:
        # Commit even when a later gate fails. The publishes that already succeeded
        # must not be re-sent next sweep -- without this, gate A delivers, gate B
        # fails, and A lands on the phone a second time.
        commit(ledger, current, updates)
    print(
        f"notify-gate: env={env} type={notification_type} "
        f"delivered {len(delivered)} -- " + "; ".join(delivered)
    )


if __name__ == "__main__":
    main()
