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
              `interrupt_class: pick`, `producer_kind: session`) offering ONE option marked
              recommended; answered with that option, in the manager's own voice

One JSON line per `--item`, carrying `verdict`:

  answerable  every mechanical gate passed; for `permission`, `tier` is `allowlist`
              (answer allow) or `manager` (apply the rubric to `command` against
              `task_file`); for `pick`, judge `recommended` against `task_file`
  escalate    leave the card for the operator; `why` names the gate that refused
  skip        nothing to answer (card not open, no park in the wrapper, a store or
              wrapper read failed) — the card, if any, stays exactly as it is

⚠️ **Fail closed, every branch.** Each check below refuses on an unreadable input
rather than passing it: an answer this session cannot justify is the act the
answerer exists never to take, and a refusal costs one card the operator was going
to see anyway.

Checks, in order (a `pick` card skips 6–7 and runs P1–P3 instead):

  1. the store card is readable and `open`
  2. it is a `permission` card, or a session's non-hook `pick` card — anything else
     (an `approve` item, a hook menu modal, a cron card) is left for its own owner
  3. it names a producing session
  4. that session carries no operator hold (`session-holds.json`)
  5. this session owns it: `own-park-check.py`'s spawn-edge verdict, or an
     ownership claim this session holds (`ownership-claim.py`'s store)
  6. the wrapper holds a parked `permission` question for that session
  7. the tier is not `operator` — classified from BOTH the wrapper's full tool input
     and the card payload, the stricter one winning, so a payload the store cut
     cannot hide what the full input shows and vice versa
  8. exactly one vault page stamps that session as its `claude_session_id` — the
     brief the rubric reads; none or several means the brief cannot be read
  P1. exactly one option is marked recommended (`recommended: true`, or a label
      carrying `(recommended`) — none or several is the operator's pick
  P2. nothing on the card — payload, context, any option label — names an
      operator-only class: prod or a deploy, a merge or release, a delete, a force
      push, secrets or credentials, a live trade, a TDR, or an `approve:` (`PICK_DENY`)

`--answer` re-runs every check at answer time and, only on `answerable`, answers the
card itself through `attention-answer.py` — `--decision allow` for a permission card,
`--answer-text "Manager answer, via supervisor: <recommended label>" --answerer manager`
for a pick — so no label ever passes through a shell the model composed.

Usage:
  gate-answer-check.py --self <manager-session-id> --item <store-item-id> [--item …]
  gate-answer-check.py --self <manager-session-id> --item <store-item-id> --answer

Exit: 0 when every item got a verdict line · 2 on a usage error (no `--self`, no item,
`--answer` with more than one item) · with `--answer`: `attention-answer.py`'s own exit,
or 4 when the re-check no longer reads `answerable` (nothing was answered).
"""

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))

STORE = os.environ.get("ATTENTION_STORE_URL", "http://localhost:18080").rstrip("/")
WRAPPER = os.environ.get("CLAUDE_WRAPPER_URL", "http://localhost:18090").rstrip("/")
HOLDS = os.environ.get("SUPERVISOR_SESSION_HOLDS") or os.path.expanduser(
    "~/.claude/state/session-holds.json")
TIMEOUT = float(os.environ.get("ATTENTION_STORE_TIMEOUT", "3"))

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
    ("approve", r"\bapprove:"),
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
    """The more restrictive of two `{tier, form, reason}` readings."""
    return a if _RANK.get(a.get("tier"), 2) >= _RANK.get(b.get("tier"), 2) else b


def command_text(question):
    """The parked call's input as one string — the command for Bash, else the JSON."""
    inp = question.get("input")
    if question.get("tool_name") == "Bash" and isinstance(inp, dict):
        return str(inp.get("command") or "")
    return json.dumps(inp, sort_keys=True) if inp is not None else ""


def decide(item_id, item, self_id, ledger, claims, holds, sessions, task_files,
           tier_mod, allowlist, own_mod):
    """One verdict dict. Every input that could not be read arrives as `None`.

    `item` is the store record, `None` when unreadable, or `{}` when absent (404).
    `sessions` is the wrapper's session list; `task_files` the vault pages stamping
    the producer.
    """
    out = {"item_id": item_id, "kind": None, "verdict": SKIP, "why": "", "tier": None,
           "form": None, "tool": None, "command": None, "command_truncated": False,
           "file_path": None, "session_id": None, "task_file": None, "cwd": None,
           "recommended": None, "options": None, "payload": None, "context": None}

    def done(verdict, why):
        out["verdict"], out["why"] = verdict, why
        return out

    if item is None:
        return done(SKIP, "store unreadable")
    if not item:
        return done(SKIP, "not a store item")
    if item.get("state") != "open":
        return done(SKIP, "card not open (%s)" % (item.get("state") or "no state"))
    mech = item.get("answer_mechanism")
    if mech == "message":
        out["kind"] = "pick"
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

    if task_files is None:
        return done(ESCALATE, "brief unreadable: vault pages could not be walked")
    if len(task_files) != 1:
        return done(ESCALATE, "brief unreadable: %d vault pages stamp %s" % (len(task_files), producer[:8]))
    out["task_file"] = str(task_files[0])
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
    if verdict.get("kind") == "pick" and verdict.get("recommended"):
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self", dest="self_id", default="")
    parser.add_argument("--item", action="append", default=[])
    parser.add_argument("--answer", action="store_true",
                        help="re-check ONE item and, only when answerable, answer it")
    args = parser.parse_args(argv)
    if not args.self_id or not args.item:
        parser.error("pass --self <this session id> and at least one --item")
    if args.answer and len(args.item) != 1:
        parser.error("--answer takes exactly one --item")

    tier_mod = _load("permission_tier", "permission-tier.py")
    own_mod = _load("own_park_check", "own-park-check.py")
    filt = _load("gate_owner_filter", "gate-owner-filter.py")
    fleet_mod = _load("fleet_sessions", "fleet-sessions.py")
    ledger = own_mod._spawn_ledger().read_ledger()
    claims = filt.load_claims()
    holds = read_holds()
    sessions = read_sessions()
    allowlist = tier_mod.load_allowlist()
    index = stamp_index(fleet_mod)

    for item_id in args.item:
        item_id = item_id.strip()
        if item_id.startswith("hst:"):
            item_id = item_id[len("hst:"):]
        item = _get_json("%s/api/1.0/attention/%s" % (STORE, item_id))
        producer = (item or {}).get("producer_id") or ""
        pages = None if index is None else index.get(producer, [])
        verdict = decide(item_id, item, args.self_id, ledger, claims, holds,
                         sessions, pages, tier_mod, allowlist, own_mod)
        print(json.dumps(verdict))
        if args.answer:
            argv_ = answer_argv(verdict)
            if argv_ is None:
                print("NOT_ANSWERED: re-check reads %s — %s" % (verdict["verdict"], verdict["why"]))
                return 4
            sys.stdout.flush()
            return subprocess.run(argv_).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
