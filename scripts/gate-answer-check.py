#!/usr/bin/env python3
"""May THIS manager answer this gate card — and if so, what is it asking?

The mechanical half of `agents/gate-answerer.md`. The manager's gate feed prints
`hst:<store item id>  session=<id>` for a wrapper-backed worker's park; the manager
dispatches the answerer on that line, and the answerer runs this first. It decides
everything that needs no judgment, so the only thing left to the model is the
`manager`-tier rubric of `docs/permission-allowlist.md` § How a manager applies it —
(a) confined, (b) in the brief, (c) not scope-changing.

Two card kinds, one verdict line each — `kind` says which:

  permission  a wrapper-backed worker's parked tool call (`answer_mechanism: permission`)
  pick        a worker's closer `pick —` card (`answer_mechanism: message`,
              `interrupt_class: pick`, `producer_kind: session`). Answered with the recommended
              label as `--answerer manager`; the store records `answer.answerer: manager`
              and every carrier (attention-controller `pkg/pane-carrier.go`,
              `pkg/headless-answer.go` `AnswerTurnText`; claude-wrapper `pkg/board.go`
              `messageAnswer`) delivers it as `Manager answer, via supervisor: …`, never in
              the operator's voice. `PICK_ANSWERING_ENABLED` is the kill switch.

One JSON line per `--item`, carrying `verdict`:

  answerable  every mechanical gate passed; `tier` is `allowlist` (answer allow) or
              `manager` (apply the rubric to `command` against `task_file`); `expect` is
              the hash `--answer --expect` must be handed back
  escalate    leave the card for the operator; `why` names the gate that refused
  skip        nothing to answer (card not open, already handled by this manager, no park
              in the wrapper, a store or wrapper read failed, picks disabled)

⚠️ **Fail closed, every branch.** Each check refuses on an unreadable input rather than
passing it: an answer this session cannot justify is the act the answerer exists never to
take, and a refusal costs one card the operator was going to see anyway.

⚠️ **Identity is not an argument.** `--self` must equal `CLAUDE_CODE_SESSION_ID`, and the
environment's value is the one ownership is checked against — a model that passed another
manager's id would otherwise answer that manager's workers. A missing variable refuses.

Permission checks, in order:

  0. this manager has not already checked this item (the dedup ledger, below)
  1. the store card is readable and `open`
  2. it is a `permission` card
  3. it names a producing session, which is not `role: human` (the operator's own workspace)
  4. that session carries no operator hold (`session-holds.json`)
  5. this session owns it: `own-park-check.py`'s spawn-edge verdict, or an ownership claim
     this session holds
  6. the wrapper holds a parked `permission` question for that session
  7. the tool is one of `UNATTENDED_TOOLS` — every `mcp__*`, `WebFetch`, `Agent`, … is the
     operator's, because the tier classifier cannot read what an MCP call reaches
  8. the card is not older than the park it would settle, and both carry a timestamp
  9. the tier is `allowlist` or `manager` — read from BOTH the wrapper's full input and the
     card payload, the stricter winning; any other tier reads as `operator`
 10. a `Bash` command is shown in full, and runs no script file (`bash x.sh`, `python3 f.py`,
     `./x`) — a file's body is code no check here has read
 11. exactly one vault page stamps the session as its `claude_session_id` — the brief

**The dedup ledger** (`~/.claude/state/gate-answer/<self-sid>.json`): a feed re-arm re-emits
every still-open gate, so without it each re-arm dispatches the answerer again for a card it
already judged — or already left for the operator. Every item that reached `answerable` or
`escalate` is recorded on first check and is `skip` for its lifetime after that; a `skip`
(a transient read failure) is not recorded, so the next emission retries it.

**`--answer`** re-checks ONE item and answers it only when (a) this manager checked it first,
(b) the re-check still reads `answerable`, and (c) the question hashes to the `--expect` the
check printed — `sha256(request_id + tool + input)`. Then it runs `attention-answer.py answer
<id> --decision allow`.

⚠️ **(c) binds the JUDGMENT to the re-check, not the answer to the park.** The store answer carries
no `request_id` that its carrier honours: claude-wrapper's `BoardDelivery.answerFor`
(`pkg/board.go`) settles the producer's CURRENT park, refusing only a card created more than 5 s
before that park. A worker that drops the judged call and parks a new one in the moment between
this re-check and the daemon's poll can receive the allow. Closing that needs the request id
carried end to end — the store's answer route already accepts `request_id`; `board.go` must
compare it with the park's before it settles.

Usage:
  gate-answer-check.py --self <this session id> --item <store-item-id> [--item …] [--dry]
  gate-answer-check.py --self <this session id> --item <store-item-id> --answer --expect <hash> [--by <provenance>]

`--dry` classifies without recording a ledger entry. A pre-answer `Monitor` needs exactly that:
`--item` books every `answerable`/`escalate` card as `checked`, and a booked card is `skip` for
this manager's lifetime — so a Monitor that probed every open card would starve the `manager`-tier
cards the dispatched gate-answerer must answer. Probe with `--dry`, then check-then-answer only
the rows it read `answerable` + `tier: allowlist`.

`--by` names the answering path the store records, and must stay under `supervisor:` — a manager
never answers in the operator's name.

Exit: 0 when every item got a verdict line · 2 on a usage error or an identity refusal · with
`--answer`: `attention-answer.py`'s own exit, or 4 when nothing was answered (the re-check, the
ledger or the hash refused).
"""

import argparse
import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))

STORE = os.environ.get("ATTENTION_STORE_URL", "http://localhost:18080").rstrip("/")
WRAPPER = os.environ.get("CLAUDE_WRAPPER_URL", "http://localhost:18090").rstrip("/")
STATE_ROOT = os.path.realpath(os.path.expanduser("~/.claude/state"))


def confined(env_name, default):
    """An env override, honoured only when it resolves under `~/.claude/state` — else the default.

    ⚠️ Both paths this script reads or writes come from the environment of the process the
    model runs, so an unconfined override is a write anywhere the user can write: a ledger
    pointed at `~/.claude` overwrites `settings.json`. Tests patch the module constants instead.
    """
    value = os.environ.get(env_name)
    if not value:
        return default
    real = os.path.realpath(os.path.expanduser(value))
    return real if real == STATE_ROOT or real.startswith(STATE_ROOT + os.sep) else default


HOLDS = confined("SUPERVISOR_SESSION_HOLDS", os.path.expanduser("~/.claude/state/session-holds.json"))
TIMEOUT = float(os.environ.get("ATTENTION_STORE_TIMEOUT", "3"))
LEDGER_DIR = confined("SUPERVISOR_GATE_ANSWER_DIR", os.path.expanduser("~/.claude/state/gate-answer"))

#: A Claude session id. `--self` names a file under `LEDGER_DIR`, so anything else is refused
#: before it reaches a path — `../../settings` would otherwise name `~/.claude/settings.json`.
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

#: Write-class tools, whose target path is what rubric clause (a) is about.
WRITE_TOOLS = ("Write", "Edit", "MultiEdit")

#: Launchers that run a program named in their arguments — the named program is unseen code.
#: `make`, `npm`/`bun`/`yarn` scripts, `go run`/`go test` and `cargo` stay `manager` tier ON
#: PURPOSE: they build or test the worker's own worktree, which is what most briefs name, and
#: rubric clause (a) still confines what they may write. Treating them as script files would
#: escalate nearly every build a worker runs.
RUNNER_WORDS = ("uv", "uvx", "npx", "pnpx", "bunx", "pipx", "poetry", "pdm", "hatch", "rye")

#: The kill switch for pick answering. On since every carrier renders an `answerer: manager`
#: answer in the manager's voice (attention-controller v0.104+, claude-wrapper v0.45+); an
#: older carrier would voice it as the operator, so turn this off if either is rolled back.
PICK_ANSWERING_ENABLED = True
PICK_DISABLED_WHY = "pick answering disabled by the PICK_ANSWERING_ENABLED kill switch"

#: The only tools an unattended manager answer may release. The tier classifier understands
#: these; an `mcp__*` call, `WebFetch` or `Agent` reaches something it cannot read.
UNATTENDED_TOOLS = ("Bash", "Write", "Edit", "MultiEdit", "Read", "NotebookRead")

#: How far a card may predate the park it would settle — claude-wrapper's own `FreshnessSkew`.
FRESHNESS_SKEW_SECONDS = 5.0

#: How long a ledger entry is kept. Store item ids are unique per card, so this bounds the file,
#: not the dedup: a card still open after a week is the operator's by then anyway.
LEDGER_TTL_SECONDS = 7 * 24 * 3600

#: Interpreters whose file operand is a program no check here has read.
INTERPRETERS = ("sh", "bash", "zsh", "dash", "ksh", "fish", "python", "python3", "node",
                "deno", "bun", "perl", "ruby", "php", "tsx", "ts-node")
#: Words that run the command after them, skipped to reach the real command word.
WRAPPER_WORDS = ("env", "time", "nohup", "exec", "nice", "command", "builtin")
SCRIPT_EXT = re.compile(r"\.(sh|bash|zsh|py|js|mjs|cjs|ts|rb|pl|php)$")

ANSWERABLE = "answerable"
ESCALATE = "escalate"
SKIP = "skip"

#: How much of the parked call's input the verdict line carries. The tier is classified on
#: the WHOLE input; this bounds only what the model reads, and a cut is flagged so a `Bash`
#: command the answerer cannot see in full is escalated rather than judged on a prefix.
COMMAND_CAP = 2000

#: The manager's own voice. A manager answering as the operator is a forged provenance claim
#: (`docs/fleet-surface.md` § The two prefixes are not interchangeable).
MANAGER_PREFIX = "Manager answer, via supervisor:"

#: Who the store records as the answerer. A pre-answer `Monitor` is NOT the gate-answerer
#: sub-agent, and the provenance must say which path settled the card — the pre-answer
#: criterion reads `answered_by` back out of the store, so a hardcoded value would make every
#: row look like the sub-agent answered it. Every value stays under the `supervisor:` namespace:
#: a manager never answers in the operator's name.
DEFAULT_ANSWERED_BY = "supervisor:gate-answerer"

#: A pick touching any of these is the operator's, whatever its recommendation. Matched
#: case-insensitively over the payload, the context and every option label — fail closed:
#: a false hit costs one card the operator was going to see anyway.
PICK_DENY = (
    ("prod/deploy", r"\bprod(uction)?\b|-prod\b|\bdeploy|make (apply|buca)|nuke-deploy|\brollout\b"),
    ("merge/release", r"\bmerg(e|es|ed|ing)\b|\brelease|\btag\b|\bpublish"),
    ("delete", r"\bdelet|\brm -|\bdrop\b|\bpurge|\bwipe|\bdestroy"),
    ("force-push", r"force[- ]?push|--force|reset --hard"),
    ("secrets", r"secret|credential|password|\btokens?\b|api[- ]?key|teamvault|keychain|\.env\b"),
    ("live-trade", r"\btrad(e|es|ing)\b|live account|\bbroker"),
    ("tdr", r"\bTDR\b|40 Decisions"),
    ("approve", r"\bapprov"),
    ("push", r"\bpush"),
    ("outbound", r"\bcomment|\be-?mail|\bpost(s|ed|ing)?\b|\brepl(y|ies|ied)\b|\bsend"),
    ("runtime", r"kubectl|launchctl|\brestart|\bkill"),
    ("disposition", r"\babort|\bcomplet(e|es|ed|ing)\b|\bhold\b|\bdefer"),
)

#: How much of a pick's payload and context the verdict line carries.
PICK_TEXT_CAP = 1500

#: Severity order, so the stricter of two readings wins.
_RANK = {"allowlist": 0, "manager": 1, "operator": 2}


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def stricter(a, b):
    """The more restrictive of two `{tier, form, reason}` readings; an unknown tier ranks top."""
    return a if _RANK.get(a.get("tier"), 3) >= _RANK.get(b.get("tier"), 3) else b


def resolve_self(arg_self, environ=None):
    """`(session_id, refusal)` — the env's session id when `--self` matches it, else a refusal."""
    env = (os.environ if environ is None else environ).get("CLAUDE_CODE_SESSION_ID", "")
    if not env:
        return None, "CLAUDE_CODE_SESSION_ID is unset, so this session cannot prove who it is"
    if not UUID_RE.match(env):
        return None, "CLAUDE_CODE_SESSION_ID is not a session id"
    if arg_self != env:
        return None, "--self does not match CLAUDE_CODE_SESSION_ID; ownership is never taken from an argument"
    return env, None


def question_hash(question):
    """What `--expect` must match: the parked call's request id, tool and full input."""
    body = json.dumps({"request_id": question.get("request_id"), "tool": question.get("tool_name"),
                       "input": question.get("input")}, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def pick_hash(item):
    body = json.dumps({"item_id": item.get("item_id"), "payload": item.get("payload"),
                       "options": item.get("options")}, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def parse_ts(value):
    """A timezone-aware datetime, or None for a missing or unparsable stamp."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        stamp = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else None


def freshness_refusal(item, question):
    """Why the card cannot be tied to THIS park, or "" when it can."""
    created, requested = parse_ts(item.get("created_at")), parse_ts(question.get("requested_at"))
    if created is None or requested is None:
        return "freshness unverifiable: card created_at or park requested_at missing"
    if (requested - created).total_seconds() > FRESHNESS_SKEW_SECONDS:
        return "card predates the park it would settle — an answer for an earlier gate"
    return ""


SHELLS = ("sh", "bash", "zsh", "dash", "ksh", "fish")


def _inline_body_safe(body, tier_mod, depth):
    """An inline `-c` body is safe only when it is itself allowlist-tier and runs no script."""
    try:
        reading = tier_mod.tier_for("Bash", {"command": body}, tier_mod.load_allowlist())
    except Exception:
        return False
    return reading.get("tier") == "allowlist" and not runs_script_file(body, tier_mod, depth + 1)


def _argv_runs_script(argv, tier_mod, depth):
    """True when this one simple command hands unseen code to something that runs it."""
    while argv and os.path.basename(re.split(r"[<>]", argv[0])[0]) in WRAPPER_WORDS:
        head = os.path.basename(argv[0])
        rest = argv[1:]
        if head == "env" and any(a == "-S" or a.startswith(("-S", "--split-string")) for a in rest):
            return True  # `env -S 'interp file'` re-splits a string into a command
        argv = rest
        while argv and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[0]) or argv[0].startswith("-")):
            argv = argv[1:]
    if not argv:
        return False
    word = argv[0]
    if "<" in word:
        return True  # `bash<x.sh`: an unspaced redirect feeds a file to the command word
    base = os.path.basename(word)
    if re.fullmatch(r"python3(\.\d+)?", base):
        base = "python3"
    if base == "xargs":
        # Option values (`-n 1`, `-I {}`) make the command's position unknowable, so ANY
        # argument that names an interpreter, a runner or a path script counts.
        for a in argv[1:]:
            b = os.path.basename(a)
            if re.fullmatch(r"python3(\.\d+)?", b) or b in INTERPRETERS or b in RUNNER_WORDS \
                    or a.startswith(("./", "../", "~/")) or SCRIPT_EXT.search(a):
                return True
        return False
    if base == "find":
        return any(a in ("-exec", "-execdir", "-ok", "-okdir") for a in argv[1:])
    if base in RUNNER_WORDS:
        return True
    if base == "git":
        args = argv[1:]
        for i, a in enumerate(args):
            value = args[i + 1] if a == "-c" and i + 1 < len(args) else (a[2:] if a.startswith("-c") and len(a) > 2 else None)
            if value is not None and ("!" in value or value.lower().startswith("alias.")):
                return True
        return False
    if base in INTERPRETERS:
        letters = "e" if base in ("node", "deno", "bun", "perl", "ruby", "php") else "c"
        index = tier_mod._inline_program_index(argv, letters)
        if index is not None:
            # An inline program: a shell body is judged like any command, and must be
            # allowlist-tier to pass; any other language's body is code no check here reads.
            if base in SHELLS and index < len(argv):
                return not _inline_body_safe(argv[index], tier_mod, depth)
            return True
        rest = argv[1:]
        if "-m" in rest or "-" in rest or any(not a.startswith("-") for a in rest) or not rest:
            return True
        return False
    return word.startswith(("./", "../", "~/")) or ("/" in word and bool(SCRIPT_EXT.search(word)))


def runs_script_file(cmd, tier_mod, depth=0):
    """True when any segment runs code no check here has read.

    That is: an interpreter handed a FILE, a module (`-m`) or stdin; an inline `-c`/`-e`
    program, unless it is a shell body that is itself allowlist-tier; a path script (`./x`,
    `dir/x.sh`); an unspaced redirect into the command word (`bash<x.sh`); and the launchers
    `xargs <cmd>`, `find -exec/-execdir`, `env -S`, `uv run`/`npx`/… and `git -c alias.*` /
    `git -c …=!…`. `make`, package-manager scripts and `go run` deliberately do not count —
    see `RUNNER_WORDS`.

    Raises nothing: a command the lexer cannot take apart reads as True, the conservative
    direction.
    """
    if depth > 8:
        return True
    try:
        segments, nested, _ = tier_mod.lex(cmd)
    except Exception:
        return True
    for inner in nested:
        if runs_script_file(inner, tier_mod, depth + 1):
            return True
    for segment in segments:
        # ⚠️ Read on the RAW segment: the tier lexer unwraps `env` itself, so `env -S 'x y'`
        # arrives as the single word `x y` and the flag that re-splits it is gone.
        if re.search(r"(^|\s)env\s(.*\s)?(-S|--split-string)", segment):
            return True
        try:
            _, argv = tier_mod.words(segment)
        except Exception:
            return True
        if any(re.search(r"\s", a) for a in argv[:1]):
            return True  # a command word holding whitespace is a string some launcher re-splits
        if _argv_runs_script(list(argv), tier_mod, depth):
            return True
    return False


def write_target_refusal(tool, inp, cwd):
    """Why a write-class call's target is outside the worker's own space, or ""."""
    if tool not in WRITE_TOOLS:
        return ""
    path = inp.get("file_path") if isinstance(inp, dict) else None
    if not isinstance(path, str) or not path.strip():
        return "write target unreadable"
    if not cwd:
        return "worker cwd unknown — the write target cannot be confined"
    home = os.path.expanduser("~")
    target = os.path.expanduser(path.strip())
    if not os.path.isabs(target):
        target = os.path.join(cwd, target)
    target = os.path.realpath(target)
    claude = os.path.realpath(os.path.join(home, ".claude"))
    if target == claude or target.startswith(claude + os.sep):
        return "write into ~/.claude is operator-only"
    roots = [os.path.realpath(cwd), os.path.realpath("/tmp")]
    if not any(target == r or target.startswith(r + os.sep) for r in roots if r != os.sep):
        return "write target %s is outside the worker's cwd and /tmp" % path
    return ""


def command_text(question):
    """The parked call's input as one string — the command for Bash, else the JSON."""
    inp = question.get("input")
    if question.get("tool_name") == "Bash" and isinstance(inp, dict):
        return str(inp.get("command") or "")
    return json.dumps(inp, sort_keys=True) if inp is not None else ""


def _role_human(session_id, ledger):
    """`gate-owner-filter.py`'s `is_human` rule: only an explicit `role: human` counts."""
    return bool(session_id) and ((ledger or {}).get(session_id) or {}).get("role") == "human"


def decide(item_id, item, self_id, ledger, claims, holds, sessions, task_files,
           tier_mod, allowlist, own_mod, scope=None, seen=None, answering=False, expect=None,
           is_human=None, live=None, max_tier="operator"):
    """One verdict dict. Every input that could not be read arrives as `None`.

    `item` is the store record, `None` when unreadable, or `{}` when absent (404).
    `sessions` is the wrapper's session list; `task_files` the vault pages stamping
    the producer; `seen` this manager's dedup ledger (`{item_id: entry}`). `answering`
    is the `--answer` re-check, which REQUIRES a prior check entry and an `expect` match.
    """
    out = {"item_id": item_id, "kind": None, "verdict": SKIP, "why": "", "tier": None,
           "form": None, "tool": None, "command": None, "command_truncated": False,
           "file_path": None, "session_id": None, "task_file": None, "cwd": None,
           "expect": None, "recommended": None, "options": None, "payload": None,
           "context": None}
    seen = seen or {}
    is_human = is_human or _role_human

    def done(verdict, why):
        out["verdict"], out["why"] = verdict, why
        return out

    entry = seen.get(item_id)
    if answering:
        if not entry or entry.get("state") != "checked" or entry.get("verdict") != ANSWERABLE:
            return done(SKIP, "not checked answerable by this manager first (ledger: %s)"
                        % ((entry or {}).get("state") or "no entry"))
    elif entry:
        return done(SKIP, "already handled by this manager (%s %s)"
                    % (entry.get("state"), entry.get("verdict")))

    if item is None:
        return done(SKIP, "store unreadable")
    if not item:
        return done(SKIP, "not a store item")
    if item.get("state") != "open":
        return done(SKIP, "card not open (%s)" % (item.get("state") or "no state"))
    mech = item.get("answer_mechanism")
    if mech == "message":
        out["kind"] = "pick"
        if not PICK_ANSWERING_ENABLED:
            return done(SKIP, PICK_DISABLED_WHY)
        refusal = pick_shape_refusal(item)
        if refusal:
            return done(ESCALATE, refusal)
    elif mech == "permission":
        out["kind"] = "permission"
    else:
        return done(ESCALATE, "not a permission or pick card (%s)" % (mech or "no mechanism"))
    producer = item.get("producer_id") or ""
    out["session_id"] = producer or None
    if not producer:
        return done(ESCALATE, "card names no producing session")
    if is_human(producer, ledger):
        return done(ESCALATE, "role: human — the operator's own workspace, never a manager's")
    if holds is None:
        return done(ESCALATE, "hold store unreadable")
    if producer in holds:
        return done(ESCALATE, "session carries an operator hold")

    own, why = own_mod.verdict(self_id, producer, ledger, scope=scope)
    if not own:
        if (claims or {}).get(producer) == self_id and self_id:
            why = "adopted by this session (ownership claim)"
        else:
            # Auto-adopt a dead-spawner gate — operator-approved 2026-11-19, card
            # 0a5572c2fcb2edae48e00277b277cb55: "auto-adopt for dead-spawner gates only,
            # rubric-limited". A gate whose spawner is not live would otherwise wait for a
            # manager turn (or time out) because no live spawner owns it. Adoption does NOT
            # bypass the rubric: every later check (UNATTENDED_TOOLS, tier allowlist/manager,
            # no script file, readable brief) still runs, so a production-touching or
            # operator-tier prompt is refused below. FAIL CLOSED: `live is None` (registry
            # unreadable) or an unnameable spawner escalates, never adopts.
            spawner = own_mod.spawner_of((ledger or {}).get(producer))
            if spawner and self_id and live is not None and spawner not in live:
                why = "adopted: spawner %s not live" % spawner[:8]
            else:
                return done(ESCALATE, "not this session's worker: %s" % why)

    if out["kind"] == "pick":
        out["expect"] = pick_hash(item)
        if answering and expect != out["expect"]:
            return done(SKIP, "expect mismatch: the card changed since it was judged")
        return decide_pick(item, out, done, why, task_files, producer)

    if sessions is None:
        return done(SKIP, "wrapper session list unreadable")
    row = next((s for s in sessions if isinstance(s, dict) and s.get("id") == producer), None)
    if row is None:
        return done(SKIP, "wrapper holds no session %s" % producer[:8])
    question = row.get("question")
    if not isinstance(question, dict) or not question.get("request_id"):
        return done(SKIP, "wrapper holds no parked question for %s" % producer[:8])
    if question.get("kind") != "permission":
        return done(ESCALATE, "wrapper park is a %s, not a permission" % (question.get("kind") or "typeless"))
    out["tool"] = question.get("tool_name")
    if out["tool"] not in UNATTENDED_TOOLS:
        return done(ESCALATE, "tool not unattended-answerable: %s" % (out["tool"] or "unnamed"))
    card_tool, _ = tier_mod.parse_payload(item.get("payload"))
    if card_tool != out["tool"]:
        return done(ESCALATE, "card names tool %s but the park is %s" % (card_tool, out["tool"]))
    stale = freshness_refusal(item, question)
    if stale:
        return done(ESCALATE, stale)
    text = command_text(question)
    out["command"], out["command_truncated"] = text[:COMMAND_CAP], len(text) > COMMAND_CAP
    inp = question.get("input")
    if isinstance(inp, dict) and inp.get("file_path"):
        out["file_path"] = str(inp["file_path"])
    out["cwd"] = row.get("cwd") or None

    full = tier_mod.tier_for(question.get("tool_name"), question.get("input") or {}, allowlist)
    card = tier_mod.tier_for_payload(item.get("payload"), allowlist)
    reading = stricter(full, card)
    out["tier"], out["form"] = reading.get("tier"), reading.get("form")
    if reading.get("tier") == "operator":
        return done(ESCALATE, "operator tier: %s" % reading.get("reason"))
    if reading.get("tier") not in ("allowlist", "manager"):
        return done(ESCALATE, "unknown tier %r reads as operator" % reading.get("tier"))
    # A caller that may only settle the mechanical tier states that ceiling here, so the
    # invariant is enforced at EVERY acting step rather than only at its own probe. The tier
    # is read from the parked call AND the card payload, and the `expect` hash covers only the
    # call — so a payload edit between probe and answer can raise the tier while the hash still
    # matches. SKIP is the right verdict: it books nothing, so the card stays available to the
    # gate-answerer that is allowed to apply the rubric.
    if _RANK.get(reading.get("tier"), 3) > _RANK.get(max_tier, 3):
        return done(SKIP, "tier %r is above this path's ceiling %r — left for the gate-answerer"
                    % (reading.get("tier"), max_tier))
    if out["command_truncated"]:
        return done(ESCALATE, "%s input truncated at %d chars — not judgeable in full" % (out["tool"], COMMAND_CAP))
    outside = write_target_refusal(out["tool"], inp, out["cwd"])
    if outside:
        return done(ESCALATE, outside)
    if out["tool"] == "Bash" and runs_script_file(text, tier_mod):
        return done(ESCALATE, "Bash runs a script file — unseen code")

    if task_files is None:
        return done(ESCALATE, "brief unreadable: vault pages could not be walked")
    if len(task_files) != 1:
        return done(ESCALATE, "brief unreadable: %d vault pages stamp %s" % (len(task_files), producer[:8]))
    out["task_file"] = str(task_files[0])
    out["expect"] = question_hash(question)
    if answering and expect != out["expect"]:
        return done(SKIP, "expect mismatch: the parked call changed since it was judged")
    return done(ANSWERABLE, "%s; %s" % (why, reading.get("reason")))


def pick_shape_refusal(item):
    """Why a `message` card is not a pick this answerer may take, or "" when it is."""
    if item.get("interrupt_class") != "pick":
        return "message card is %s, not a pick" % (item.get("interrupt_class") or "unclassed")
    if item.get("producer_kind") != "session":
        return "pick raised by %s, not a worker session" % (item.get("producer_kind") or "no producer kind")
    if item.get("provenance_class") == "hook" or item.get("questions"):
        return "a hook menu (modal) card — answered by keystroke, not by this path"
    return ""


def option_labels(item):
    labels = []
    for opt in item.get("options") or []:
        label = opt.get("label") if isinstance(opt, dict) else opt
        if isinstance(label, str) and label.strip():
            labels.append(label.strip())
    return labels


def recommended_option(item):
    """The ONE recommended label, or None when none or several are marked."""
    flagged = [str(o.get("label")).strip() for o in item.get("options") or []
               if isinstance(o, dict) and o.get("recommended") is True and o.get("label")]
    if not flagged:
        flagged = [label for label in option_labels(item) if "(recommended" in label.lower()]
    return flagged[0] if len(flagged) == 1 else None


def pick_deny_class(texts):
    """The first `PICK_DENY` class any text names, or None."""
    joined = "\n".join(t for t in texts if isinstance(t, str))
    for name, pattern in PICK_DENY:
        if re.search(pattern, joined, re.IGNORECASE):
            return name
    return None


def decide_pick(item, out, done, own_why, task_files, producer):
    labels = option_labels(item)
    out["options"] = labels
    out["payload"] = str(item.get("payload") or "")[:PICK_TEXT_CAP]
    out["context"] = str(item.get("context") or "")[:PICK_TEXT_CAP]
    choice = recommended_option(item)
    if choice is None:
        return done(ESCALATE, "no single recommended option")
    out["recommended"] = choice
    hit = pick_deny_class([item.get("payload"), item.get("context")] + labels)
    if hit:
        return done(ESCALATE, "pick touches an operator-only class: %s" % hit)
    if task_files is None:
        return done(ESCALATE, "brief unreadable: vault pages could not be walked")
    if len(task_files) != 1:
        return done(ESCALATE, "brief unreadable: %d vault pages stamp %s" % (len(task_files), producer[:8]))
    out["task_file"] = str(task_files[0])
    return done(ANSWERABLE, "%s; one recommended option, no operator-only class" % own_why)


def answer_argv(verdict, by=DEFAULT_ANSWERED_BY):
    """`attention-answer.py` argv for an `answerable` verdict, or None for anything else."""
    if verdict.get("verdict") != ANSWERABLE:
        return None
    base = [sys.executable, os.path.join(_HERE, "attention-answer.py"), "answer",
            verdict["item_id"], "--by", by]
    if verdict.get("kind") == "permission":
        return base + ["--decision", "allow"]
    if verdict.get("kind") == "pick" and verdict.get("recommended") and PICK_ANSWERING_ENABLED:
        return base + ["--answer-text", "%s %s" % (MANAGER_PREFIX, verdict["recommended"]),
                       "--answerer", "manager"]
    return None


def _get_json(url):
    """Parsed body, `{}` on a 404, `None` on any other failure."""
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as err:
        return {} if err.code == 404 else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def read_holds(path=None):
    try:
        with open(path or HOLDS, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    holds = data.get("holds") if isinstance(data, dict) else None
    return holds if isinstance(holds, dict) else None


def read_sessions():
    data = _get_json(WRAPPER + "/api/1.0/sessions")
    if isinstance(data, dict) and isinstance(data.get("sessions"), list):
        return data["sessions"]
    return data if isinstance(data, list) else None


def stamp_index(fleet_mod):
    """`{session_id: [page paths]}` over every vault page's `claude_session_id`, or None.

    One walk for the whole run — it reads every vault, so walking once per item made a
    four-item run cost four full walks (measured 2026-11-18: 16 s for six items).
    """
    try:
        index = {}
        for sid, path, _ in fleet_mod._walk_task_stamps():
            index.setdefault(sid, []).append(path)
        return index
    except Exception:
        return None


def ledger_path(self_id, root=None):
    if not isinstance(self_id, str) or not UUID_RE.match(self_id):
        raise ValueError("ledger key is not a session id: %r" % (self_id,))
    return os.path.join(root or LEDGER_DIR, "%s.json" % self_id)


class Ledger:
    """This manager's dedup ledger, held under an exclusive lock for one run.

    The lock spans read → decide → write, so two answerers dispatched for the same item (a
    `--digest` line and a re-arm) cannot both find it unseen.
    """

    def __init__(self, path):
        self.path, self.items, self._lock = path, {}, None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._lock = open(self.path + ".lock", "a")
        fcntl.flock(self._lock, fcntl.LOCK_EX)
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            items = data.get("items") if isinstance(data, dict) else None
            self.items = items if isinstance(items, dict) else {}
        except FileNotFoundError:
            self.items = {}
        except (OSError, ValueError):
            # ⚠️ An unreadable ledger is not an empty one: every item reads as already
            # handled, so nothing is judged twice — the run answers nothing instead.
            self.items = None
        return self

    def record(self, item_id, **fields):
        if self.items is None:
            return
        fields["at"] = time.time()
        self.items[item_id] = fields

    def forget(self, item_id):
        """Drop one entry, returning the card to every other answerer.

        A booked card is `skip` for this manager's lifetime, so a path that books a card and
        then fails to answer it would strand that gate permanently — the dispatched
        gate-answerer would read `skip` and never look again. Releasing is the only repair.
        """
        if self.items is None:
            return
        self.items.pop(item_id, None)

    def save(self):
        if self.items is None:
            return
        cutoff = time.time() - LEDGER_TTL_SECONDS
        kept = {k: v for k, v in self.items.items() if isinstance(v, dict) and v.get("at", 0) >= cutoff}
        tmp = "%s.%d.tmp" % (self.path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"items": kept}, fh)
        os.replace(tmp, self.path)

    def __exit__(self, *exc):
        try:
            self.save()
        finally:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
            self._lock.close()
        return False


class _AllSeen(dict):
    """The `seen` view of an unreadable ledger: every item is already handled."""

    def get(self, key, default=None):
        return {"state": "ledger-unreadable", "verdict": "unknown"}


def load_inputs(self_id=""):
    """Every store and file the decision reads — one seam, so tests can replace it whole.

    `scope` is the durable half of the ownership predicate: the vault's answer to "whose work
    is this", which is what lets a manager answer a card raised by a PEER's worker. It is
    loaded once per invocation — the answer cannot change within a run — and an empty
    `self_id` yields an unresolved Scope that admits nothing.

    ⚠️ **This is a SECOND full vault walk, on top of `stamp_index`'s.** Measured 2026-11-18,
    one walk costs ~3 s over both vaults, so a check now pays roughly twice the walk it did
    before. Acceptable for a background agent, but it is the obvious thing to fix if the
    answerer's latency ever matters: `stamp_index` and `session-scope.load()` want the same
    pages, and sharing one walk is a contained follow-up.
    """
    tier_mod = _load("permission_tier", "permission-tier.py")
    own_mod = _load("own_park_check", "own-park-check.py")
    filt = _load("session_ownership", "session-ownership.py")
    fleet_mod = _load("fleet_sessions", "fleet-sessions.py")
    scope_mod = _load("session_scope", "session-scope.py")
    return {
        "tier_mod": tier_mod, "own_mod": own_mod, "is_human": filt.is_human,
        "ledger": own_mod._spawn_ledger().read_ledger(), "claims": filt.load_claims(),
        "holds": read_holds(), "sessions": read_sessions(),
        "allowlist": tier_mod.load_allowlist(), "index": stamp_index(fleet_mod),
        "scope": scope_mod.load(self_id),
        # Live session ids from the Claude Code registry, or None when unreadable. Used only
        # to decide whether a gate's spawner is dead (auto-adoption, below); None means
        # unknown, and adoption refuses rather than guessing.
        "live": filt.live_ids(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self", dest="self_id", default="")
    parser.add_argument("--item", action="append", default=[])
    parser.add_argument("--answer", action="store_true",
                        help="re-check ONE item and, only when answerable, answer it")
    parser.add_argument("--expect", default="",
                        help="with --answer: the `expect` hash the check printed")
    parser.add_argument("--dry", action="store_true",
                        help="classify WITHOUT recording a ledger entry — the pre-answer probe")
    parser.add_argument("--by", default=DEFAULT_ANSWERED_BY,
                        help="with --answer: the provenance the store records (must be supervisor:*)")
    parser.add_argument("--max-tier", dest="max_tier", default="operator",
                        choices=("allowlist", "manager", "operator"),
                        help="refuse (without booking) any card above this tier — a caller that "
                             "may only settle the mechanical tier passes `allowlist`")
    parser.add_argument("--release", action="store_true",
                        help="drop this item's ledger entry, returning the card to other answerers")
    args = parser.parse_args(argv)
    if not args.by.startswith("supervisor:"):
        parser.error("--by must start with 'supervisor:' — a manager never answers in the operator's name")
    if args.release and len(args.item) != 1:
        parser.error("--release takes exactly one --item")
    if not args.self_id or not args.item:
        parser.error("pass --self <this session id> and at least one --item")
    if args.answer and (len(args.item) != 1 or not args.expect):
        parser.error("--answer takes exactly one --item and its --expect hash")
    self_id, refusal = resolve_self(args.self_id)
    if refusal:
        print("REFUSED: %s" % refusal)
        return 2

    if args.release:
        item_id = args.item[0].strip()
        if item_id.startswith("hst:"):
            item_id = item_id[len("hst:"):]
        with Ledger(ledger_path(self_id)) as book:
            book.forget(item_id)
        print(json.dumps({"item_id": item_id, "released": True}))
        return 0

    inputs = load_inputs(self_id)
    index = inputs["index"]
    with Ledger(ledger_path(self_id)) as book:
        seen = _AllSeen() if book.items is None else book.items
        for item_id in args.item:
            item_id = item_id.strip()
            if item_id.startswith("hst:"):
                item_id = item_id[len("hst:"):]
            item = _get_json("%s/api/1.0/attention/%s" % (STORE, item_id))
            producer = (item or {}).get("producer_id") or ""
            pages = None if index is None else index.get(producer, [])
            verdict = decide(item_id, item, self_id, inputs["ledger"], inputs["claims"],
                             inputs["holds"], inputs["sessions"], pages, inputs["tier_mod"],
                             inputs["allowlist"], inputs["own_mod"],
                             scope=inputs.get("scope"), seen=seen,
                             answering=args.answer, expect=args.expect or None,
                             is_human=inputs["is_human"], live=inputs.get("live"),
                             max_tier=args.max_tier)
            print(json.dumps(verdict))
            if not args.answer:
                if verdict["verdict"] in (ANSWERABLE, ESCALATE) and not args.dry:
                    book.record(item_id, state="checked", verdict=verdict["verdict"],
                                expect=verdict["expect"], why=verdict["why"])
                continue
            argv_ = answer_argv(verdict, by=args.by)
            if argv_ is None:
                print("NOT_ANSWERED: re-check reads %s — %s" % (verdict["verdict"], verdict["why"]))
                return 4
            # Recorded BEFORE the answer runs: a second `--answer` for this item, from any
            # answerer, now finds `answering` and refuses, even if this process dies mid-call.
            book.record(item_id, state="answering", verdict=ANSWERABLE, expect=verdict["expect"])
            book.save()
            sys.stdout.flush()
            rc = subprocess.run(argv_).returncode
            book.record(item_id, state="answered", verdict=ANSWERABLE, expect=verdict["expect"], rc=rc)
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
