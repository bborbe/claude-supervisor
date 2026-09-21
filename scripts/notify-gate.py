#!/usr/bin/env python3
"""Publish manager ACTION gates to the notification core, with cadence de-dup.

stdin: JSON {"gates": [{"owner": "<session id or pane id>", "text": "<gate line>"}]}
       An empty list is a clean sweep -- it prunes and sends nothing.

Why this exists: a manager that raises an ACTION gate reaches only an operator who
is at the machine or listening, because TTS cannot leave the room. This publishes
the same gate to Telegram, so a gate raised while the operator is away arrives.

Three de-dup axes, and this script owns the second and third:

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
  * WHO ALREADY RAISED IT is the cross-layer stamp -- the counterweight to that
    per-layer split, added 2026-09-21. The split is right for cadence and is what
    lets two layers escalate the SAME gate with neither aware: the fleet drops a
    gate a live worker manager owns, but the worker manager and the fleet can both
    hold the same underlying question, and each stamps its own layer's ledger, so
    neither sees the other. Measured twice on 2026-09-20 (Fleet Manager session
    `b700c650`): two duplicates, both costing the operator a decision, one of them
    also leaving a worker parked. The stamp records which session escalated a gate
    and suppresses a SECOND session's escalation of it -- deliberately NOT the same
    session's, which must still re-raise on its own cadence or a manager would
    deadlock against its own stamp. See stamp_of()/write_stamp().

Cadence, decided 2026-09-19: once on raise, then re-raise at 1h and 4h while the
gate is still open, capped at 3 deliveries per gate identity. After the third,
silence until the gate changes or clears.

TWO identities, and they are deliberately different -- see stamp_key() for why:

  * CADENCE identity is (owner, normalised text). Normalising collapses whitespace
    so a re-rendered line is not mistaken for a new gate; a genuinely different text
    IS a new identity, because the question changed and the operator should hear it.
    Safe to include `owner` here because the ledger is per layer -- both halves are
    always compared within one view.
  * STAMP identity is the normalised TEXT ALONE. `owner` is unpinned across layers:
    both manager commands document it as `<session id or pane id>` and leave the
    choice to the manager, so the same logical gate can carry a session id from one
    layer and a pane id from the other. Keyed on the cadence identity those two
    never match, every gate double-fires exactly as before, and the defect reads as
    fixed -- a silent no-op, which is worse than no fix because it stops anyone
    looking.

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

Stamps: ~/.claude/state/gate-stamps/<hash>.json, ONE FILE PER GATE and shared
across layers (override SUPERVISOR_GATE_STAMP_DIR). Deliberately not the ledger:
the ledger is pruned to the sweep's own gates, so a stamp kept there would be
deleted by the other layer's sweep -- the exact layer that must still see it.
One file per gate rather than one shared file, because this is the first record
in this script that two layers contend on and per-gate files need no cross-layer
lock: each is written by one process at a time and renamed into place.
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
import subprocess
import sys

CONFIG_PATH = os.environ.get("SUPERVISOR_CONFIG") or os.path.expanduser(
    "~/.config/claude-supervisor/config.json"
)
STATE_PATH = None  # set by main() from --layer; see ledger_path()
STAMP_DIR = None  # set by main(); see stamp_path() and the module docstring
LAYER = None  # set by main(); recorded on each stamp for diagnostics only


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


def stamp_dir(layer):
    """The cross-layer stamp directory -- deliberately NOT per layer.

    The exact opposite of ledger_path() above, and for a reason that is the mirror
    of it: the ledger is per layer because "absent from this sweep" must mean
    "cleared for this layer", but the stamp's whole job is to be visible to the
    layer that did NOT raise the gate. Scoping it per layer would reproduce the
    defect it exists to fix, one directory deeper.

    `layer` is accepted and unused so the call site reads like ledger_path()'s and
    a future per-layer need is a one-line change rather than a signature change.
    """
    override = os.environ.get("SUPERVISOR_GATE_STAMP_DIR")
    if override:
        return override
    return os.path.expanduser("~/.claude/state/gate-stamps")


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

# How long a cross-layer stamp suppresses another session's escalation of the same
# gate. Reuses the cadence's FIRST rung rather than inventing a second contract: past
# it the escalating session would itself be due a re-raise, so a stamp that still
# suppressed would outlive the evidence that its owner is doing anything about the
# gate. Re-exported as SUPERVISOR_GATE_STAMP_TTL for the tests only.
STAMP_TTL = 3600

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


def stamp_key(gate):
    """The cross-layer stamp identity: the gate's normalised TEXT, and not `owner`.

    Deliberately NOT `gate_key(owner, text)`, which is the cadence identity. The two
    differ because they answer different questions, and using the cadence key here
    would ship a fix that looks right and never fires:

      * `gate_key` answers "is this the same gate *for cadence*", where `owner`
        disambiguates two sessions whose gates read alike. It is safe there because
        the ledger is per layer -- both halves are compared within one view.
      * The stamp answers "is another manager already asking the operator this",
        which is a question about the QUESTION. `owner` is unpinned across layers:
        both manager commands document it as `<session id or pane id>` and leave the
        choice to the manager, so the same logical gate can carry a session id from
        one layer and a pane id from the other. Keyed on `gate_key`, those two never
        match, every gate double-fires exactly as before, and the defect reads as
        fixed -- the silent no-op.

    `normalise` is what makes the text usable as this key, and its own docstring
    already says so: it exists so "a re-rendered gate keeps one identity".

    The cost, stated rather than hidden: two genuinely different gates whose text
    normalises identically within one TTL would collide, and the second would be
    suppressed. Narrow -- the text is the question, so identical text means the
    operator is being asked the same thing -- but real, and it is why `owner` is
    kept in the record: the skip line quotes it, so a collision is visible.
    """
    return normalise(gate.get("text") or "")


def stamp_path(key):
    """The stamp file for a gate identity.

    Hashed rather than named, because a `gate_key` carries a `\\x1f` separator and
    arbitrary gate prose -- neither survives as a filename, and a sanitised name
    would collide two different gates onto one stamp, which fails as a SILENT
    suppression of a real gate.
    """
    return os.path.join(STAMP_DIR, hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")


def load_stamp(key):
    """This gate's stamp, or None when it has none.

    Unreadable reads as None for the same reason load_ledger() reads an unreadable
    ledger as empty: the cost is one duplicate escalation, and a traceback here
    would take out the gate notification entirely.
    """
    try:
        with open(stamp_path(key), encoding="utf-8") as handle:
            stamp = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(stamp, dict) or not stamp.get("session_id"):
        return None
    try:
        parse_time(stamp["ts"])
    except (KeyError, TypeError, ValueError):
        return None
    return stamp


def write_stamp(layer, session_id, now, gate):
    """Record that `session_id` escalated this gate.

    `owner`/`text` are stored for DIAGNOSTICS ONLY -- the skip line quotes the gate
    text so the manager can see which question it is declining to repeat. The
    DECISION is made on `session_id` alone; never on the stored prose, which is a
    copy that could drift from the gate it describes.

    Written to a temp file and renamed, the same discipline commit() uses, so a
    process dying mid-write cannot leave a half-written stamp for the next sweep
    to read as a suppression.
    """
    try:
        os.makedirs(STAMP_DIR, exist_ok=True)
    except OSError as error:
        sys.exit(f"notify-gate: cannot create {STAMP_DIR}: {error}")
    path = stamp_path(stamp_key(gate))
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "session_id": session_id,
                    "layer": layer,
                    "ts": format_time(now),
                    "owner": normalise(gate.get("owner") or ""),
                    "text": normalise(gate.get("text") or ""),
                },
                handle,
                indent=2,
                sort_keys=True,
            )
        os.replace(tmp, path)
    except OSError as error:
        # Loud, not silent: a stamp that failed to write means the duplicate this
        # exists to prevent comes back, and nothing else would say so.
        sys.exit(f"notify-gate: cannot write {path}: {error}")


def clear_stamp(key):
    """Drop a gate's stamp. Used by the tests and by `stamp_of`'s expiry path.

    Deliberately NOT called for every gate absent from a sweep, which is the one
    place this could go badly wrong: a layer's sweep is a partial view by design
    (`ledger_path()`), so "absent from MY sweep" does NOT mean the gate cleared --
    the fleet drops every gate a live worker manager owns. Clearing on absence
    would let the fleet's sweep delete the worker's stamp, which is precisely the
    cross-layer blindness this whole mechanism exists to remove.

    So a stamp is released by its TTL instead, and the directory stays bounded
    because `load_stamp` unlinks a stamp the moment it reads one as expired.
    """
    try:
        os.unlink(stamp_path(key))
    except OSError:
        pass


def stamp_of(gate, now):
    """The stamp suppressing this gate, or None when it is this session's to raise.

    Takes the GATE rather than a precomputed key, so the identity is derived in
    exactly one place (`stamp_key`) and no caller can pass the cadence `gate_key`
    here by mistake -- the one error that would make this whole mechanism a no-op.

    Returns None in three distinct cases, and they are NOT interchangeable:

      * no stamp            -- nobody has raised it; this session should.
      * own stamp           -- this session already raised it, so it must be free to
                               re-raise on its own cadence. Treating an own stamp as
                               a suppression would deadlock the manager against
                               itself on every later sweep -- the mirror-image bug
                               that makes a boolean flag worse than no flag at all.
      * stale stamp         -- older than STAMP_TTL. A stamp must not suppress
                               forever: if the escalating session dies or never
                               relays, an unbounded stamp means the gate is never
                               escalated by anyone again, which is strictly worse
                               than the duplicate this prevents. Expiry degrades
                               the failure to a DELAYED re-escalation.

    A stale stamp is unlinked on the way out, so expiry is also the garbage
    collector -- the directory cannot grow one file per gate ever seen.
    """
    key = stamp_key(gate)
    stamp = load_stamp(key)
    if stamp is None:
        return None
    try:
        raised = parse_time(stamp["ts"])
    except (KeyError, TypeError, ValueError):
        clear_stamp(key)
        return None
    if (now - raised).total_seconds() >= STAMP_TTL:
        clear_stamp(key)
        return None
    if stamp["session_id"] == escalating_session_id():
        return None
    return stamp


def escalating_session_id():
    """The session id of the manager running this escalation, or "".

    Empty means the identity is unknown, which is reported rather than papered
    over: a stamp written with a blank identity cannot distinguish "escalated by
    me" from "escalated by someone else", so it would reintroduce the boolean bug
    in another costume. The gate is still escalated -- this never fails closed on
    a real gate -- and the degradation is named in the output.
    """
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "")


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
    global STATE_PATH, STAMP_DIR, LAYER
    args = parse_args(sys.argv[1:])
    STATE_PATH = ledger_path(args.layer)
    STAMP_DIR = stamp_dir(args.layer)
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

    # Cross-layer stamp: drop any gate a DIFFERENT session already escalated. Applied
    # before `decide()` so a suppressed gate is not counted as due, and so the
    # suppression is visible in the output rather than folded into a cadence count.
    suppressed = {}
    for key in list(current):
        stamp = stamp_of(current[key], now)
        if stamp is not None:
            suppressed[key] = stamp
            del current[key]

    due = [
        (key, gate) for key, gate in current.items() if decide(ledger.get(key), now)
    ]

    me = escalating_session_id()
    # A suppressed gate that this session did NOT raise is the duplicate this exists
    # to prevent. Reported in every case -- a silent skip is indistinguishable from
    # a dropped gate, which is the failure mode this whole task is about.
    for key, stamp in suppressed.items():
        print(
            f"notify-gate: skipping '{normalise(stamp.get('text') or '')[:60]}' -- "
            f"already escalated by session {stamp['session_id']} "
            f"({stamp.get('layer') or 'unknown layer'})"
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
        print(
            f"notify-gate: {len(current) + len(suppressed)} open, "
            f"{len(suppressed)} suppressed, 0 due"
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
            if me:
                write_stamp(LAYER, me, now, gate)
            delivered.append(f"{gate['owner']} ({deliveries}/{MAX_DELIVERIES})")
    finally:
        # Commit even when a later gate fails. The publishes that already succeeded
        # must not be re-sent next sweep -- without this, gate A delivers, gate B
        # fails, and A lands on the phone a second time.
        commit(ledger, current, updates, me)
    tail = f", {len(suppressed)} suppressed" if suppressed else ""
    print(
        f"notify-gate: env={env} type={notification_type} "
        f"delivered {len(delivered)}{tail} -- " + "; ".join(delivered)
    )


if __name__ == "__main__":
    main()
