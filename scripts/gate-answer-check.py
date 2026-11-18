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
              `interrupt_class: pick`, `producer_kind: session`). ⚠️ **DISABLED**
              (`PICK_ANSWERING_ENABLED = False`): every carrier that delivers a card answer
              renders it as the OPERATOR's (attention-controller `pkg/pane-carrier.go`,
              `pkg/headless-answer.go` `AnswerTurnText`; claude-wrapper `pkg/board.go`
              `messageAnswer`), so a manager's pick would reach the worker as an operator
              answer. Every pick card is `skip` until the carriers render the manager's voice.

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
check printed — `sha256(request_id + tool + input)`, so the call answered is the call judged.
Then it runs `attention-answer.py answer <id> --decision allow`.

Usage:
  gate-answer-check.py --self <this session id> --item <store-item-id> [--item …]
  gate-answer-check.py --self <this session id> --item <store-item-id> --answer --expect <hash>

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
HOLDS = os.environ.get("SUPERVISOR_SESSION_HOLDS") or os.path.expanduser(
    "~/.claude/state/session-holds.json")
TIMEOUT = float(os.environ.get("ATTENTION_STORE_TIMEOUT", "3"))
LEDGER_DIR = os.environ.get("SUPERVISOR_GATE_ANSWER_DIR") or os.path.expanduser(
    "~/.claude/state/gate-answer")

#: Off until every carrier renders a manager's answer in the manager's voice — see the module
#: docstring. The pick branch below stays, tested with this flipped, so turning it on is one line.
PICK_ANSWERING_ENABLED = False
PICK_DISABLED_WHY = "pick answering disabled until carriers render manager voice"

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


def runs_script_file(cmd, tier_mod, depth=0):
    """True when any segment hands a FILE (or stdin) to an interpreter, or runs a path script.

    Raises nothing: a command the lexer cannot take apart is `operator` tier already, and is
    refused before this runs; here it reads as True, the conservative direction.
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
        try:
            _, argv = tier_mod.words(segment)
        except Exception:
            return True
        while argv and os.path.basename(argv[0]) in WRAPPER_WORDS:
            argv = [a for a in argv[1:]]
            while argv and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[0]) or argv[0].startswith("-")):
                argv = argv[1:]
        if not argv:
            continue
        word = argv[0]
        base = os.path.basename(word)
        if re.fullmatch(r"python3(\.\d+)?", base):
            base = "python3"
        if base in INTERPRETERS:
            letters = "e" if base in ("node", "deno", "bun", "perl", "ruby", "php") else "c"
            if tier_mod._inline_program_index(argv, letters) is not None:
                continue
            rest = argv[1:]
            if "-m" in rest or "-" in rest or any(not a.startswith("-") for a in rest) or not rest:
                return True
            continue
        if word.startswith(("./", "../", "~/")) or ("/" in word and SCRIPT_EXT.search(word)):
            return True
    return False


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
           tier_mod, allowlist, own_mod, seen=None, answering=False, expect=None,
           is_human=None):
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

    own, why = own_mod.verdict(self_id, producer, ledger)
    if not own:
        if (claims or {}).get(producer) == self_id and self_id:
            why = "adopted by this session (ownership claim)"
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
    if out["tool"] == "Bash" and out["command_truncated"]:
        return done(ESCALATE, "Bash command truncated at %d chars — not judgeable in full" % COMMAND_CAP)
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


def answer_argv(verdict):
    """`attention-answer.py` argv for an `answerable` verdict, or None for anything else."""
    if verdict.get("verdict") != ANSWERABLE:
        return None
    base = [sys.executable, os.path.join(_HERE, "attention-answer.py"), "answer",
            verdict["item_id"], "--by", "supervisor:gate-answerer"]
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


def load_inputs():
    """Every store and file the decision reads — one seam, so tests can replace it whole."""
    tier_mod = _load("permission_tier", "permission-tier.py")
    own_mod = _load("own_park_check", "own-park-check.py")
    filt = _load("gate_owner_filter", "gate-owner-filter.py")
    fleet_mod = _load("fleet_sessions", "fleet-sessions.py")
    return {
        "tier_mod": tier_mod, "own_mod": own_mod, "is_human": filt.is_human,
        "ledger": own_mod._spawn_ledger().read_ledger(), "claims": filt.load_claims(),
        "holds": read_holds(), "sessions": read_sessions(),
        "allowlist": tier_mod.load_allowlist(), "index": stamp_index(fleet_mod),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self", dest="self_id", default="")
    parser.add_argument("--item", action="append", default=[])
    parser.add_argument("--answer", action="store_true",
                        help="re-check ONE item and, only when answerable, answer it")
    parser.add_argument("--expect", default="",
                        help="with --answer: the `expect` hash the check printed")
    args = parser.parse_args(argv)
    if not args.self_id or not args.item:
        parser.error("pass --self <this session id> and at least one --item")
    if args.answer and (len(args.item) != 1 or not args.expect):
        parser.error("--answer takes exactly one --item and its --expect hash")
    self_id, refusal = resolve_self(args.self_id)
    if refusal:
        print("REFUSED: %s" % refusal)
        return 2

    inputs = load_inputs()
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
                             inputs["allowlist"], inputs["own_mod"], seen=seen,
                             answering=args.answer, expect=args.expect or None,
                             is_human=inputs["is_human"])
            print(json.dumps(verdict))
            if not args.answer:
                if verdict["verdict"] in (ANSWERABLE, ESCALATE):
                    book.record(item_id, state="checked", verdict=verdict["verdict"],
                                expect=verdict["expect"], why=verdict["why"])
                continue
            argv_ = answer_argv(verdict)
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
