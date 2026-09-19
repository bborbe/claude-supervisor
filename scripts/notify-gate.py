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
  * HOW OFTEN is this script's ledger. The manager loop re-invokes every ~15 min
    (fleet) and ~5 min (worker), so an unbounded re-raise would put one open gate
    on the phone roughly 96 times a day -- the noise the TTS gate exists to
    prevent, moved to a louder channel.

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
"""
import datetime
import json
import os
import subprocess
import sys

CONFIG_PATH = os.environ.get("SUPERVISOR_CONFIG") or os.path.expanduser(
    "~/.config/claude-supervisor/config.json"
)
STATE_PATH = os.path.expanduser("~/.claude/state/gate-notifications.json")

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


def resolve_endpoint(config):
    notify = config.get("notify")
    if not notify:
        sys.exit(
            f"notify-gate: no `notify` block in {CONFIG_PATH} and a gate is due -- "
            "refusing to skip silently. Add "
            '{"notify":{"env":"dev","endpoints":{"dev":{"baseUrl":"...",'
            '"teamvaultKey":"..."}}}} and retry.'
        )
    env = notify.get("env", "dev")
    endpoint = (notify.get("endpoints") or {}).get(env) or {}
    if not endpoint.get("baseUrl") or not endpoint.get("teamvaultKey"):
        sys.exit(
            f"notify-gate: config has no complete endpoint for env `{env}` in "
            f"{CONFIG_PATH} -- need both baseUrl and teamvaultKey."
        )
    return env, endpoint, notify.get("type", DEFAULT_TYPE)


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
    value = f"{username}:{password}".replace("\\", "\\\\").replace('"', '\\"')
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
        sys.exit(f"notify-gate: publish failed: {result.stderr.strip()}")
    return result.stdout.strip()


def commit(ledger, current, updates):
    """Keep only identities still open, then apply this sweep's updates.

    Pruning falls out of `kept`: a gate that cleared is absent from `current`, so
    its count is dropped and a later re-raise starts fresh.
    """
    kept = {key: ledger[key] for key in current if key in ledger}
    kept.update(updates)
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as handle:
        json.dump(kept, handle, indent=2, sort_keys=True)
    return kept


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = json.load(sys.stdin)
    gates = payload.get("gates") or []

    ledger = {}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as handle:
            ledger = json.load(handle)

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
            "owner": gate["owner"],
            "text": normalise(gate["text"]),
        }
        delivered.append(f"{gate['owner']} ({deliveries}/{MAX_DELIVERIES})")

    commit(ledger, current, updates)
    print(
        f"notify-gate: env={env} type={notification_type} "
        f"delivered {len(delivered)} -- " + "; ".join(delivered)
    )


if __name__ == "__main__":
    main()
