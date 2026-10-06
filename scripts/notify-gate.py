#!/usr/bin/env python3
"""Publish manager ACTION gates to the notification core, with cadence de-dup.

stdin: JSON {"gates": [{"owner": "<session id or pane id>", "text": "<gate line>",
                        "session": "<the blocked session's id>"}]}
       An empty list is a clean sweep -- it prunes and sends nothing.
       `session` is the session the gate is ABOUT -- the one whose item carries the
       stamp. It is required, because without it no store item can be resolved and
       every gate would silently lose cross-layer dedup.

Why this exists: a manager that raises an ACTION gate reaches only an operator who
is at the machine or listening, because TTS cannot leave the room. This publishes
the same gate to Telegram, so a gate raised while the operator is away arrives.

Three de-dup axes, and this script owns the second and third:

  * WHICH manager notifies is already decided upstream -- `commands/fleet-loop.md`
    drops an entry whose owning manager is live on the roster, so any given
    gate reaches this script at most once per sweep.
  * HOW OFTEN is this script's ledger -- and it is PER LAYER, because each layer's
    sweep is a partial view (the fleet drops worker-owned gates). A shared ledger
    would let one layer prune the other's gates as "cleared", after which they
    would re-raise forever. The manager loop re-invokes every ~15 min (fleet) and
    ~5 min (worker), so an unbounded re-raise would put one open gate on the phone
    roughly 96 times a day -- the noise the TTS gate exists to prevent, moved to a
    louder channel.
  * WHO ALREADY RAISED IT is the **attention store item's `escalated_by` field** --
    the counterweight to that per-layer split. The split is right for cadence and is
    what lets two layers escalate the SAME gate with neither aware: the fleet drops a
    gate a live manager owns, but the manager and the fleet can both
    hold the same underlying question, and each keeps its own layer's ledger, so
    neither sees the other. Measured twice on 2026-09-20 (Fleet Manager session
    `b700c650`): two duplicates, both costing the operator a decision, one of them
    also leaving a worker parked. The store records which session escalated an ITEM
    and suppresses a SECOND session's escalation of it -- deliberately NOT the same
    session's, which must still re-raise on its own cadence or a manager would
    deadlock against its own stamp. See store_stamp_of().

    **This replaced a local stamp ledger on 2026-09-21, and the store item is the one
    writer.** The ledger keyed on the gate's normalised TEXT alone, because `owner` is
    unpinned across layers and could not serve as part of an identity -- a workaround
    for having nowhere better to put the mark. The item is that better place: it is
    what managers actually read, and it already carries `producer_id`. The ledger is
    gone rather than kept alongside, because two writers for one mark drift; the
    accepted cost is that a gate with **no resolvable store item** loses cross-layer
    dedup, which is reported as `unresolved` rather than silently absorbed.

    The join is `producer_id == <the gate's subject session>`, and it is the only one
    available: the store's `item_id` is written by the store, the watcher's dedup key
    is the HOOK LOG's id (`sha256(session_id + tool_use_id)`, `attention-log.py`), and
    a manager holds no `tool_use_id`. So each gate must name its **subject session**
    in a `session` field -- which is NOT `owner` (a pane id in live stamps) and NOT
    the escalating session (the stamp records who escalated, not who is blocked).

Cadence, decided 2026-09-19: once on raise, then re-raise at 1h and 4h while the
gate is still open, capped at 3 deliveries per gate identity. After the third,
silence until the gate changes or clears.

ONE identity now, and it belongs to the cadence ledger alone:

  * CADENCE identity is (owner, normalised text). Normalising collapses whitespace
    so a re-rendered line is not mistaken for a new gate; a genuinely different text
    IS a new identity, because the question changed and the operator should hear it.
    Safe to include `owner` here because the ledger is per layer -- both halves are
    always compared within one view.

There is deliberately no second identity in this script any more. The cross-layer
stamp used to derive one from the gate's text, precisely because `owner` is unpinned
across layers (`<session id or pane id>`, the same logical gate carrying a session id
from one layer and a pane id from the other) and so could not anchor an identity that
had to match across two views. That derivation is retired: the stamp now lives on the
store ITEM, whose identity the store owns and whose `producer_id` is a session id by
construction. Two gates are "the same gate" for stamping purposes exactly when they
resolve to the same item.

Pruning is what makes the bound per-gate rather than per-lifetime: an identity
absent from a sweep is a gate that cleared, so it is dropped and the SAME gate can
raise again later at full cadence -- but only when the sweep that finds it absent
is the one that RAISED it. A manager's sweep is one topic's partial view, so
"absent from my sweep" is a claim about its own gates and never about another
manager's; that scoping is `owned_by_this_sweep()`.

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
-- see ledger_path() for why a shared file silently defeats the cadence. Each
entry records the escalating session as `escalatedBy`, and a sweep prunes only
entries it owns -- the layer namespaces the FILE, `escalatedBy` namespaces the
PRUNE, and both are needed because a layer holds many managers.

Stamps: the attention store item's `escalated_by`, read from `$ATTENTION_STORE_URL`
(default `http://localhost:18080`, the same var and default who-needs-me.py uses) and
written through `POST /api/1.0/attention/<item_id>/escalate`. Deliberately NOT a file
beside the ledger: the ledger is pruned to the sweep's own gates, so a stamp kept
there would be deleted by the other layer's sweep -- the exact layer that must still
see it. The store is shared across layers by construction and the item is what
managers already read, so the mark sits where its readers look rather than in a second
place they must remember to consult.
"""
import argparse
import datetime
import fcntl
import importlib.util
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, filename):
    """Import a sibling script by path — the filenames carry hyphens."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


attention_store_auth = _load("attention_store_auth", "attention-store-auth.py")

CONFIG_PATH = os.environ.get("SUPERVISOR_CONFIG") or os.path.expanduser(
    "~/.config/claude-supervisor/config.json"
)
STATE_PATH = None  # set by main() from --layer; see ledger_path()
LAYER = None  # set by main(); diagnostics only

# The attention store -- where the cross-layer stamp now lives. Same defaults and
# env var as who-needs-me.py, deliberately: both scripts read the same store, and a
# second spelling of the default would let one of them silently point elsewhere.
STORE = os.environ.get("ATTENTION_STORE_URL", "http://localhost:18080").rstrip("/")
STORE_TIMEOUT = float(os.environ.get("ATTENTION_STORE_TIMEOUT", "3"))


def ledger_path(layer):
    """The ledger is PER LAYER, and that is load-bearing rather than tidy.

    The two manager layers see different slices of the world: `fleet-loop` drops
    every gate whose owning manager is live, so its sweep is deliberately a
    subset. A shared ledger would let the fleet's sweep prune a worker-owned gate
    (absent from *its* list), after which the worker's next sweep sees that gate as
    first-sight and republishes it with a fresh count -- so a gate that stays open
    would re-notify on every fleet tick, forever, instead of at most MAX_DELIVERIES
    times. That is precisely the noise this script exists to prevent.

    Scoping the ledger to the layer makes "absent from this sweep" mean "cleared
    *for this layer*", which is the only reading that is true for a partial view.

    ⚠️ The layer is necessary and NOT sufficient, and reading this docstring as the
    whole answer is what let the defect through. `--layer worker` is not one
    manager: it is every topic manager in the fleet, all pointed at this one file.
    So "absent from this sweep" still did not mean "cleared" -- a manager whose
    sweep raised nothing published `{"gates": []}` and pruned every other manager's
    open gates, which re-notified as first-sight on their next tick, forever. The
    per-layer split is right and stays; what was missing is the per-MANAGER half,
    which lives on each entry as `escalatedBy` and is enforced in
    `owned_by_this_sweep()`. Measured 2026-09-21: the operator's live worker ledger
    held gates owned by two other topics, neither of them the sweeping manager's.
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

# There is deliberately no stamp TTL any more. The local ledger needed one because a
# stamp kept outside the item had nothing to expire with -- an escalating session that
# died would otherwise suppress the gate forever. The store item has a lifecycle of its
# own, and `escalated_by` dies with it: an item that is answered or closed leaves the
# queue, so its stamp becomes unreachable rather than stale. Re-introducing a TTL here
# would be re-deriving a bound the schema already provides.

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
    """Name the gate and its owner, with the gate's own text on a line of its own.

    The gate text is where a manager puts the command the operator is meant to run,
    and a phone wraps a long line mid-argument -- so a line-wise copy of an INLINE
    command yields an unbalanced quote and a command naming something that does not
    exist. Measured 2026-09-19: copying `/vault-cli:complete-goal "The Manager Ranks
    Work by What It Costs Me"` out of a delivered notification produced
    `/vault-cli:complete-goal "The Manager Ranks` -- truncated exactly at the wrap.

    Keeping this side's prefix off that line starts it near column zero, which is
    most of what the sender controls; the rest is the manager's own line length, so
    the runbook's ACTION-gate frame asks for commands on their own line as well.
    """
    return (
        f"Manager gate open -- {normalise(gate['owner'])}\n"
        f"{normalise(gate['text'])}\n"
        "Replies here are not read -- answer it in the owning session."
    )


def load_config():
    try:
        with open(CONFIG_PATH) as handle:
            config = json.load(handle)
    except FileNotFoundError:
        return {}
    except ValueError as error:
        sys.exit(f"notify-gate: {CONFIG_PATH} is not valid JSON: {error}")
    except OSError as error:
        # An unreadable config is a real failure the manager must see as a fix, not
        # as a traceback -- FileNotFoundError is handled above, so this is the
        # permission/IO class the docstring's "fails loudly" promise also covers.
        sys.exit(f"notify-gate: cannot read {CONFIG_PATH}: {error}")
    # The top level is guarded here for the same reason resolve_endpoint() guards
    # `notify`, `endpoints` and `endpoint` one level down: valid JSON that is not
    # an object would otherwise AttributeError on the first .get().
    if not isinstance(config, dict):
        sys.exit(
            f"notify-gate: {CONFIG_PATH} must contain a JSON object, got "
            f"{type(config).__name__} -- the shape is in the README."
        )
    return config


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


def run_command(argv, **kwargs):
    """subprocess.run, with a missing binary reported in this script's own style.

    A binary absent from PATH is the ordinary fresh-machine failure, and neither
    `teamvault-cli` nor `curl` is checked for anywhere else -- so the spawn is the
    one place that can name the fix. Left unguarded it raises FileNotFoundError and
    the manager surfaces a traceback instead of a remedy.
    """
    try:
        return subprocess.run(argv, **kwargs)
    except OSError as error:
        sys.exit(
            f"notify-gate: cannot run {argv[0]}: {error} -- is it installed and on PATH?"
        )


def teamvault_field(field, key):
    result = run_command(
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
    result = run_command(
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
    except FileNotFoundError:
        return {}
    except ValueError:
        # A truncated or corrupt ledger self-heals: every gate re-raises once and
        # the next commit rewrites the file. Reading it as empty is right here.
        return {}
    except OSError as error:
        # An IO/permission failure is a different thing entirely: it will block the
        # WRITE as well, so reading it as empty would re-raise every gate on every
        # sweep forever. Loud, not silent.
        sys.exit(f"notify-gate: cannot read {STATE_PATH}: {error}")
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
    try:
        handle = open(f"{STATE_PATH}.lock", "w")
        fcntl.flock(handle, fcntl.LOCK_EX)
    except OSError as error:
        sys.exit(f"notify-gate: cannot lock {STATE_PATH}.lock: {error}")
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


def owned_by_this_sweep(entry, me):
    """True when this sweep's manager is the one that escalated `entry`.

    This is what makes the prune scoped to a MANAGER rather than to a layer, and it
    is the fix for the defect the per-layer ledger could not see: `--layer worker`
    is not one manager, it is every topic manager in the fleet, so a manager whose
    sweep raised nothing published `{"gates": []}`, `current` came out empty, and
    `commit` dropped EVERY entry in the file -- including gates owned by other
    managers, which then re-notified as first-sight.

    An entry with no `escalatedBy` predates the field, so its owner is unknown --
    and unknown is NOT this sweep's. It is kept, which is both the conservative
    direction and the migration path: the entry is adopted the moment its own
    manager publishes it again (the update carries `escalatedBy`), and only then
    does it become prunable. Keeping it costs at most one stale entry that its
    owner re-adopts on its next sweep; claiming it here would prune exactly the
    foreign gates this function exists to protect, once, against the operator's
    live ledger during the upgrade.

    The same reading covers `escalatedBy: ""`, written by a sweep that ran with no
    identity: a blank is not a wildcard matching a later blank, so two
    identity-less managers never prune each other.
    """
    owner = entry.get("escalatedBy")
    return bool(me) and owner == me


def commit(ledger, current, updates, me):
    """Keep only identities still open AND owned by this sweep, then apply updates.

    Pruning falls out of `kept`: a gate that cleared is absent from `current`, so
    its count is dropped and a later re-raise starts fresh. The `owned_by_this_sweep`
    half is what keeps "absent from MY sweep" from reading as "cleared" for a gate
    another manager raised -- the two are different claims and only the first is
    true of a partial view.

    The loop runs over `ledger` rather than over `current`, and that is load-bearing
    rather than stylistic: iterating `current` can only ever keep keys THIS sweep
    published, so an empty sweep -- `current == {}` -- would drop the whole file no
    matter what the ownership test said. A foreign entry is precisely one that is
    absent from `current`, so it has to be reachable from the ledger side.

    `me` is passed rather than read from the environment so the identity is decided
    in one place (`escalating_session_id`, at the top of the round) instead of at
    each write, and so a caller cannot commit against an identity it did not
    escalate with.

    Written through a temp file and renamed, so no reader ever sees a half-written
    ledger even if this process dies mid-dump.
    """
    kept = {
        key: entry
        for key, entry in ledger.items()
        if key in current or not owned_by_this_sweep(entry, me)
    }
    kept.update(updates)
    ensure_state_dir()
    tmp = f"{STATE_PATH}.tmp"
    try:
        with open(tmp, "w") as handle:
            json.dump(kept, handle, indent=2, sort_keys=True)
        os.replace(tmp, STATE_PATH)
    except OSError as error:
        # This runs from the `finally` in publish_round, so it cannot be swallowed
        # -- but it must still read as a fix rather than a traceback.
        sys.exit(f"notify-gate: cannot write {STATE_PATH}: {error}")
    return kept


def store_items():
    """Every item the store currently holds, or None when the store is unreachable.

    None is deliberately distinct from []: an unreachable store and an empty queue
    both mean "no item to stamp", but only one of them is a degraded run worth
    saying out loud. Collapsing them would make a store outage read as a clean
    sweep, which is the silent-success shape this whole task exists to remove.
    """
    try:
        with urllib.request.urlopen(
            attention_store_auth.request(STORE + "/api/1.0/attention"), timeout=STORE_TIMEOUT
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "[]")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if isinstance(payload, dict):
        payload = payload.get("items") or []
    if not isinstance(payload, list):
        return None
    return payload


def gate_item(gate, items):
    """The store item this gate is about, or None when none can be resolved.

    The join is `producer_id == <the gate's subject session>`, and it is the only one
    available: the store owns `item_id`, and the watcher's dedup key is the HOOK LOG's
    id (`sha256(session_id + tool_use_id)`, attention-log.py), which a manager has no
    way to compute -- it reads a pane, not an event.

    `session` is the SUBJECT session, and neither of the two ids already in a gate
    will do: `owner` is a pane id in live stamps, and the escalating session is the
    manager itself, recorded on the stamp rather than used to find the item.

    When a session has more than one open item, the gate `text` is matched against the
    item `payload` as a TIEBREAK ONLY, and a weak one: the manager paraphrases the
    gate while `payload` is the hook's own `detail`. So a tiebreak matching nothing
    returns None -- the same answer as no item at all -- rather than falling back to
    the first or to all of them. A tiebreak that quietly picks wrong is worse than one
    that reports it could not pick, which is the rule the schema's silence 7 already
    sets for every other unresolvable value in this stack.
    """
    session = normalise(gate.get("session") or "")
    if not session:
        return None
    mine = [item for item in items if item.get("producer_id") == session]
    if not mine:
        return None
    if len(mine) == 1:
        return mine[0]
    wanted = normalise(gate.get("text") or "")
    for item in mine:
        if normalise(item.get("payload") or "") == wanted:
            return item
    return None


def store_stamp_of(item, me):
    """What suppresses this gate, or None when it is this session's to raise.

    Takes the already-resolved ITEM rather than the gate, so the join is made once per
    gate and the caller can act on the same item it just resolved -- stamping a
    different item than the one whose stamp was read is the one way this could go
    quietly wrong.

    Returns a dict naming the suppressing session, or None in two cases that are NOT
    interchangeable -- the caller reports them differently, and folding them together
    is what would turn a lost gate into a quiet one:

      * no stamp on the item -- nobody has raised it; this session should.
      * own stamp           -- this session already raised it, so it must stay free to
                               re-raise on its own cadence. Treating an own stamp as a
                               suppression would deadlock the manager against itself on
                               every later sweep, the mirror-image bug that makes a
                               boolean flag worse than no flag.

    The third case -- no resolvable item at all -- is decided by the CALLER, because it
    is the caller that must report it as `unresolved` rather than read it as "no stamp".

    There is no expiry check here, and its absence is deliberate rather than an
    omission: the item's own answered/closed transition is the expiry, so a stamp on
    an item that has left the queue is unreachable rather than stale.
    """
    raised_by = item.get("escalated_by") or ""
    if not raised_by or raised_by == me:
        return None
    return {"session_id": raised_by, "item_id": item.get("item_id") or ""}


def stamp_item(item_id, session_id):
    """Record on the store item that `session_id` is carrying it.

    Returns None on success or a short reason string on failure, and never raises: a
    stamp that could not be written means the duplicate this exists to prevent comes
    back, and that must be REPORTED rather than take out the notification -- the gate
    itself still has to reach the operator.
    """
    if not item_id:
        return "no item id to stamp"
    body = json.dumps({"escalated_by": session_id}).encode("utf-8")
    request = urllib.request.Request(
        f"{STORE}/api/1.0/attention/{item_id}/escalate",
        data=body,
        headers=attention_store_auth.headers({"Content-Type": "application/json"}),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=STORE_TIMEOUT) as resp:
            resp.read()
        return None
    except urllib.error.HTTPError as error:
        # 409 is a lost race rather than a failure: another session stamped the item
        # between this round's read and this write, which is the outcome the stamp
        # exists to produce.
        if error.code == 409:
            return "lost the race to another session"
        return f"store returned HTTP {error.code}"
    except (urllib.error.URLError, OSError) as error:
        return f"store unreachable ({error})"

def escalating_session_id():
    """The session id of the manager running this escalation, or "".

    Empty means the identity is unknown, which is reported rather than papered
    over: a stamp written with a blank identity cannot distinguish "escalated by
    me" from "escalated by someone else", so it would reintroduce the boolean bug
    in another costume. The gate is still escalated -- this never fails closed on
    a real gate -- and the degradation is named in the output.
    """
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "")


HOLDS_PATH = os.path.expanduser(
    os.environ.get("SUPERVISOR_SESSION_HOLDS", "~/.claude/state/session-holds.json")
)


def read_holds():
    """The operator's session holds, keyed on session id. `{}` on any read failure.

    Inlined rather than shelling out to session-holds.py: the plugin's scripts do not
    import each other, and a subprocess dependency on a plugin path is a fail-open.
    Reads are lock-free because the writer lands every change through `os.replace`.

    A missing or corrupt store reads as *nothing held*, and that direction is chosen:
    inventing a hold would silence a real escalation, while reading a real store as
    empty merely fails to honour a hold.
    """
    try:
        with open(HOLDS_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    holds = data.get("holds") if isinstance(data, dict) else None
    return holds if isinstance(holds, dict) else {}


def is_held(session_id):
    """True when this session carries a hold. Never raises."""
    if not session_id:
        return False
    return isinstance(read_holds().get(session_id), dict)


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
        for field in ("owner", "text", "session"):
            if not gate.get(field):
                sys.exit(
                    f"notify-gate: a gate is missing `{field}`: {json.dumps(gate)} "
                    '-- each gate needs {"owner": "...", "text": "...", '
                    '"session": "..."}, where `session` is the BLOCKED session and '
                    "not the escalating one. It is the only key that resolves the "
                    "store item, so without it the gate loses cross-layer dedup."
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
    global STATE_PATH, LAYER
    args = parse_args(sys.argv[1:])
    STATE_PATH = ledger_path(args.layer)
    LAYER = args.layer

    now = datetime.datetime.now(datetime.timezone.utc)
    gates = read_gates()
    # Held for the whole round and released when the file closes, so the read, the
    # publishes and the commit form one critical section rather than three.
    with lock_ledger():
        publish_round(now, gates)


def publish_round(now, gates):
    ledger = load_ledger()
    current = {gate_key(g["owner"], g["text"]): g for g in gates}

    # The escalating identity is read BEFORE the stamp lookup, because the lookup needs
    # it: a stamp only suppresses when it belongs to someone else.
    me = escalating_session_id()

    # A held session's gates are never published. This runs BEFORE the store read and
    # independently of it, because a hold is local operator policy: it must apply even
    # when the store is unreachable, unlike the cross-layer de-dup below, which needs
    # the store and degrades without it.
    #
    # ⚠️ The gate leaves `current` (the publish set) but its store item is left ALONE --
    # the same shape `suppressed` uses. Pruning the item would make a RELEASED hold
    # re-notify as though the gate were new, which inverts the design: a hold suppresses
    # the message, never the record.
    held = {}
    for key in list(current):
        gate = current[key]
        if is_held(gate.get("session") or ""):
            held[key] = gate
            del current[key]

    # Read the store ONCE per round rather than once per gate -- the join is a scan of
    # the item list and a sweep raises a handful of gates. `None` means the store was
    # unreachable, reported rather than read as "no stamps anywhere": the two are
    # different, and only one of them is a degraded run.
    items = store_items()
    if items is None:
        print(
            f"notify-gate: attention store unreachable at {STORE} -- escalating "
            "WITHOUT cross-layer de-dup; a gate another layer already raised will be "
            "sent again"
        )

    # Drop any gate a DIFFERENT session already escalated. Applied before `decide()` so
    # a suppressed gate is not counted as due, and so the suppression is visible in the
    # output rather than folded into a cadence count.
    suppressed = {}
    unresolved = []
    item_ids = {}
    for key in list(current):
        if items is None:
            break
        gate = current[key]
        item = gate_item(gate, items)
        if item is None:
            # No item, so no stamp can be READ and none can be WRITTEN. The retired
            # ledger covered this case; naming it out loud is the point of having
            # retired it rather than a gap this change quietly introduced.
            unresolved.append(gate)
            continue
        item_ids[key] = item.get("item_id") or ""
        stamp = store_stamp_of(item, me)
        if stamp is not None:
            suppressed[key] = dict(stamp, text=gate.get("text") or "")
            del current[key]

    due = [
        (key, gate) for key, gate in current.items() if decide(ledger.get(key), now)
    ]
    # A suppressed gate that this session did NOT raise is the duplicate this exists
    # to prevent. Reported in every case -- a silent skip is indistinguishable from
    # a dropped gate, which is the failure mode this whole task is about.
    for stamp in suppressed.values():
        print(
            f"notify-gate: skipping '{normalise(stamp.get('text') or '')[:60]}' -- "
            f"already escalated by session {stamp['session_id']} "
            f"(item {stamp.get('item_id') or 'unknown'})"
        )
    # A held gate is reported for the same reason a suppressed one is: a silent skip is
    # indistinguishable from a dropped gate, and that ambiguity is what this feature
    # exists to remove. The row stays visible; only the message stops.
    for gate in held.values():
        print(
            f"notify-gate: HELD '{normalise(gate.get('text') or '')[:60]}' -- session "
            f"{normalise(gate.get('session') or '')[:8]} carries an operator hold; "
            "not published"
        )
    # A gate with no resolvable item has NO cross-layer de-dup, and that is a real loss
    # rather than a clean skip: two layers can now both send it. Named per gate, because
    # silence here is indistinguishable from a gate that was deduped correctly -- and
    # this is precisely the case the retired ledger used to cover.
    for gate in unresolved:
        print(
            f"notify-gate: unresolved '{normalise(gate.get('text') or '')[:60]}' -- "
            f"no open store item for session {normalise(gate.get('session') or '')}; "
            "cross-layer de-dup is NOT in force for this gate"
        )
    if not me and (suppressed or due):
        # Degraded, not broken: without an identity a stamp cannot distinguish
        # "escalated by me" from "escalated by someone else", so none is written.
        print(
            "notify-gate: CLAUDE_CODE_SESSION_ID unset -- escalating WITHOUT a "
            "stamp; a duplicate of this gate will not be suppressed"
        )

    if not due:
        commit(ledger, current, {}, me)
        lost = f", {len(unresolved)} unresolved" if unresolved else ""
        print(
            f"notify-gate: {len(current) + len(suppressed)} open, "
            f"{len(suppressed)} suppressed{lost}, 0 due"
        )
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
                # Who owns this identity, for `owned_by_this_sweep` at the next
                # commit. `me` is written as-is (empty included): a blank value is
                # NOT a wildcard, so an entry published without an identity is
                # simply never pruned by anyone, which fails safe. Writing the
                # gate's `owner` here instead would make ownership depend on a
                # field the commands document as "<session id or pane id>" and
                # leave to the manager -- unstable by construction, and it would
                # let one manager prune another's gate whenever the two agree.
                "escalatedBy": me,
            }
            # Stamped AFTER a successful publish, never before: a stamp written for a
            # gate that failed to send would suppress the other layer's attempt to
            # raise it, turning a delivery failure into a gate nobody escalates.
            # Only a gate that RESOLVED to an item can be stamped. An unresolved gate
            # has no item to write to, and calling the store with an empty id would
            # report a stamping failure for a case the run already reports as
            # `unresolved` -- two lines for one fact, and the louder one misleading.
            if me and item_ids.get(key):
                failure = stamp_item(item_ids[key], me)
                if failure:
                    # Reported, never swallowed: an unstamped gate is one the other
                    # layer will raise a second time, and nothing else would say so.
                    print(
                        "notify-gate: could not stamp "
                        f"'{normalise(gate['text'])[:60]}' -- {failure}"
                    )
            delivered.append(f"{gate['owner']} ({deliveries}/{MAX_DELIVERIES})")
    finally:
        # Commit even when a later gate fails. The publishes that already succeeded
        # must not be re-sent next sweep -- without this, gate A delivers, gate B
        # fails, and A lands on the phone a second time.
        commit(ledger, current, updates, me)
    tail = f", {len(suppressed)} suppressed" if suppressed else ""
    tail += f", {len(unresolved)} unresolved" if unresolved else ""
    print(
        f"notify-gate: env={env} type={notification_type} "
        f"delivered {len(delivered)}{tail} -- " + "; ".join(delivered)
    )


if __name__ == "__main__":
    main()
