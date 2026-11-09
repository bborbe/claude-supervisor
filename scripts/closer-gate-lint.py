#!/usr/bin/env python3
"""closer-gate-lint — `Stop`: do not gate the operator on a step the rules already authorise.

WHAT IT BLOCKS, AND WHY A BLOCK RATHER THAN A WARNING. A worker that ends its turn on
`👤 You: approve: …` or `👤 You: review: …` for a step the global rules already authorise
is asking for a second approval of a decision the plan gate already took. Measured
2026-10-19, two workers in one fleet sweep:

    Worker "Update Go to 1.27.2" (pane 809):
        approve: reply `watch` — I run /github-pr-watch on imap-spam-deleter#11/#12
    Worker "Backtest Agents Publish to the Production ActualTrade Event Topic…" (pane 832):
        review: draft PR bborbe/trading#380 — reply `go` and I run the local review

Both steps are reversible, in-scope and non-production, and both are already named as
no-ask work: `~/.claude/CLAUDE.md` § After Opening a PR § PR Watch Guards prefers
`/github-pr-watch`, and § PR Bot Review Trigger orders the local review loop before
`gh pr ready`. § Execution Phase No Re-Ask performs a written, reversible,
non-production subtask with no `approve:` line. Each gate still costs the operator an
action, and gates are the binding throughput constraint.

⚠️ **The allow-list is DERIVED from those rules, not invented, and may not widen without
an operator decision.** It is the verb vocabulary of the After Opening a PR rules, plus the
no-ask verbs those rules name outright. Adding a verb is an operator decision, taken per
closer class and recorded on the vault task that measured the class.

⚠️ **`gh pr merge` is allow-listed in its plain form only** (operator decision 2026-10-30).
§ After Opening a PR § Merge on Green says in terms to "merge without asking", which makes
the plain merge no-ask in its own right; the earlier text here read § Execution Phase
No Re-Ask's do-not-weaken list as keeping every merge gated, and the two rules were left
contradicting each other. `--admin` stays gated — it bypasses branch protection — and that
exclusion lives in `DENY`, checked before `ALLOW` is walked, because `match_allow` returns
the first match: a per-pattern lookahead would let a body naming both `--admin` and
another allow-listed verb through on the other entry. Pinned by
`fixtures/closer-gate-lint/14-gh-pr-merge-admin.jsonl` and
`15-session-close-with-admin-merge.jsonl`. Compaction and
`git worktree remove` were measured in the same decision and deliberately left gated:
neither has a no-ask basis in CLAUDE.md, so allow-listing either would mean writing a new
rule first, not editing this list.

⚠️ **The matcher is verb-based and cannot verify the rule's own preconditions.** It cannot
tell whether the subtask is written in the task file, or whether a `gh pr ready` is on an
own PR. That is why the list is confined to verbs the After Opening a PR rules make no-ask
in their own right, and why an accepted false positive is the right trade: a linted
`gh pr ready` on someone else's PR costs one turn of the model being told to proceed; it
never performs a wrong action, because the model still does the work.

⚠️ **Fail-open, on every path.** This runs on `Stop` in every session that loads the
plugin. A hook that blocks, crashes, or prompts degrades the session it observes, so every
failure path here exits 0 having done nothing. The cost of a missed lint is one operator
gate; the cost of a fail-closed hook is a broken turn in every session on the machine.

⚠️ **`stop_hook_active` is a loop guard, not a courtesy — and it is bounded, not
unconditional.** Exit 2 continues the turn; a model that ends the continued turn on the
same closer would re-block forever without a guard. So the retry is allowed — with ONE
exception: when the previous `Stop` blocked this same closer and the closer is unchanged,
it is blocked once more (`blocked-retry`), and only a third identical stop is allowed.
The bound is read from this hook's own record, so the worst case is two consecutive
blocks, never a loop. Measured 2026-11-08: on the deciding 7-day window the unconditional
allow released **21** re-emitted closers naming already-authorised verbs.

Usage (from `hooks/hooks.json`, `Stop`):

    stdin: the hook payload, carrying `transcript_path` and `stop_hook_active`
    exit 0: allow the stop (also every fail-open path)
    exit 2: block the stop; stderr is shown to the model, which continues the turn

⚠️ **The record is written on ALLOW and BLOCK alike**, so an absent record means the hook
did not run rather than that nothing happened. The write is fail-open, and the record is
read by exactly one thing — the bounded retry above — where a lost record can only cost
the extra block, never a stuck turn.

SECOND CHECK — A WAIT ON SOMETHING THAT DIES WITH THE SESSION. A `🟡 WAITING` closer whose
`⏰ Ends:` names a session-bound mechanism — a background sub-agent, a Monitor, a
`run_in_background` shell, a `ScheduleWakeup`, a `CronCreate` — and no durable observable
waits on a notice that can never arrive once the session ends. Measured 2026-11-08, pane
619 (session `e6807caf`):

    ⏰ Ends:   chunk 7a and 7c completion notices (background agents)

The previous session had ended, both agents died with it, and the worker sat 23 minutes at
an empty prompt on a task unmoved for 28 hours — found only by a human reading the pane.
Nobody keeps a background sub-agent, by design (operator decision 2026-11-08: forbid it as
a wait rather than build a keeper), so `~/.claude/CLAUDE.md` § Async State Closer requires
the observable to outlive the session.

⚠️ **The observable is banned, not the mechanism.** The same rule REQUIRES the in-session
watcher in the `(mechanism, id)` slot — `/github-pr-watch` is itself a background shell — so
a closer naming a session-bound class passes whenever it also names a durable observable:
a file, a 32-hex card id, a `defer_date` / ISO date, or an external probe (a PR, CI, `gh`,
`kubectl`, a pod, a URL). Operator decision 2026-11-08 ("Observable only", card
`3d9c0b310909dc8ca406169621673957`). A literal ban would exit 2 on every compliant PR-watch
closer, which is a broken rule rather than a strict one.

⚠️ **It runs before the gate checks**, because the shape it exists for ends `👤 You: nothing`,
which the gate checks return `not-a-gate` on. It reads the `⏰ Ends:` line from the same
message `last_closer` reads, through `reap-closer.py`'s one scanner. The record carries
`class` (the matched mechanism) and `phrase` (the `⏰ Ends:` text) on every invocation —
`null` when nothing was flagged.

Kill switch: `SUPERVISOR_CLOSER_GATE_LINT=off`. State dir override:
`SUPERVISOR_CLOSER_GATE_LINT_STATE_DIR`.
"""

import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone

KILL_SWITCH_ENV = "SUPERVISOR_CLOSER_GATE_LINT"
STATE_DIR_ENV = "SUPERVISOR_CLOSER_GATE_LINT_STATE_DIR"
STATE_SUBDIR = "closer-gate-lint"

# ⚠️ Both bounds are documented because a reader depends on the shape: the file keeps its
# most recent `RECORD_KEEP` lines once it passes `RECORD_CAP_BYTES` or 200 lines, whichever
# binds first.
RECORD_CAP_BYTES = 262144
RECORD_KEEP = 200

UNKNOWN_SESSION = "unknown-session"

DECISION_ALLOW, DECISION_BLOCK = "allow", "block"

# ⚠️ The one reason that is not a fresh decision: a closer the lint already blocked, seen
# again on the continued turn's `Stop`. Bounded — see `_retry_verdict`.
RETRY_REASON = "blocked-retry"

# `approve:` / `review:` are the two gate verbs the closer contract defines. `you run:` is
# deliberately not here — a command the operator must execute is not a step the model may
# simply perform.
GATE_RE = re.compile(r"^(approve|review):\s*(.+)$", re.IGNORECASE)

# The handle check reads a WIDER verb set than the allow-list: a `you run:` that names a
# card is an instruction to poll or answer it, and an unpollable handle there freezes the
# reader exactly as it does under `review:`.
HANDLE_GATE_RE = re.compile(r"^(approve|review|you run):\s*(.+)$", re.IGNORECASE)

# A card handle is hex named AS a card — after `card`, `item` or `poll`, optionally
# backticked. ⚠️ Keyed on that word, never on bare hex: closers cite commit SHAs (7–40
# hex) all the time, and a bare-hex rule would refuse every one of them.
CARD_HANDLE_RE = re.compile(
    r"(?:\b(?:card|item)(?:[ _-]?id)?|\bpoll)\b[\s:#`'\"=]*([0-9a-f]{4,})\b", re.IGNORECASE)

# ⚠️ An all-digit run is a number (`item 202611`, a PR, a date), never a store id: the
# store mints lowercase hex, and a run with no a–f letter is overwhelmingly a count.
_HEX_LETTER_RE = re.compile(r"[a-f]", re.IGNORECASE)

# `attention-ask.py` `STORE_ITEM_ID_LEN`: `poll` refuses any other length. ⚠️ The
# predicate is "not pollable", never "not resolvable" — an 8-char prefix DOES resolve
# through `attention-card-lookup.py`, so a resolvability test would pass the very input
# this exists to refuse.
STORE_ITEM_ID_LEN = 32


def unpollable_handles(body):
    """Card handles in `body` that `poll` cannot address."""
    return [h for h in CARD_HANDLE_RE.findall(body)
            if len(h) != STORE_ITEM_ID_LEN and _HEX_LETTER_RE.search(h)]

# Derived from `~/.claude/CLAUDE.md` § After Opening a PR and § Execution Phase No Re-Ask
# § Named No-Ask Actions. ⚠️ Order is irrelevant; the name is what the record carries.
ALLOW = (
    ("github-pr-watch", re.compile(r"/github-pr-watch\b", re.IGNORECASE)),
    ("coding-local-review", re.compile(r"/coding:local-review\b", re.IGNORECASE)),
    ("coding-pr-review", re.compile(r"/coding:pr-review\b", re.IGNORECASE)),
    ("gh-pr-ready", re.compile(r"\bgh\b.*\bpr\s+ready\b", re.IGNORECASE)),
    ("dark-factory-prompt-approve", re.compile(r"\bdark-factory\s+prompt\s+approve\b", re.IGNORECASE)),
    ("dark-factory-spec-approve", re.compile(r"\bdark-factory\s+spec\s+approve\b", re.IGNORECASE)),
    ("vault-cli-session-close", re.compile(r"/vault-cli:session-close\b", re.IGNORECASE)),
    ("gh-pr-merge", re.compile(r"\bgh\b.*\bpr\s+merge\b", re.IGNORECASE)),
)

# ⚠️ A GLOBAL deny, deliberately not a lookahead inside one ALLOW entry. `match_allow`
# returns the FIRST match, so a body naming both `--admin` and another allow-listed verb
# is released by the other entry and the admin bypass slips through — measured on
# `approve: /vault-cli:session-close then gh pr merge 60 --merge --admin`, where the
# session-close entry matched first. Checking here makes the exclusion independent of
# ALLOW's order and of which verb the closer happens to name first.
DENY = (
    re.compile(r"--admin\b", re.IGNORECASE),
)

# ⚠️ The one prose alias, and it is measured rather than guessed: the observed pane-832
# closer says "I run the local review", never the verb. A verb-only matcher would miss the
# exact line this hook exists for.
PROSE_ALIASES = (
    ("coding-local-review", re.compile(r"\blocal review\b", re.IGNORECASE)),
)

# The precondition each allow-listed verb carries in the rules, named in the message.
# ⚠️ The hook cannot verify any of these — it is verb-based by construction — so the
# message says what to check rather than implying the check already happened. An entry
# absent here carries no precondition the rules attach.
WHEN = {
    "gh-pr-merge": "the PR is bot-approved and its CI is green (§ After Opening a PR § Merge on Green)",
    "gh-pr-ready": "the local review loop is clean, CI is green and origin/master is merged",
    "vault-cli-session-close": "the task is terminal on disk (§ Async State Closer)",
}


ENDS_MARKER = "⏰ Ends:"

# The five session-bound classes. ⚠️ `Monitor` is case-sensitive and must stand alone: a
# lowercase `monitor` inside a path (`~/.claude/state/monitor-check/…`) is a file, which is
# the opposite of what this class names. A Kubernetes CronJob is external, so the cron
# class is the `CronCreate` tool only.
SESSION_BOUND = (
    ("background-sub-agent", re.compile(
        r"\b(?:background\s+(?:sub-?)?agents?|sub-?agents?|agent\s+tasks?)\b", re.IGNORECASE)),
    ("monitor", re.compile(r"(?<![\w/.-])Monitor(?![\w/.-])")),
    ("run-in-background-shell", re.compile(
        r"\brun_in_background\b|\bbackground\s+(?:bash|shell)s?\b", re.IGNORECASE)),
    ("schedule-wakeup", re.compile(r"\bScheduleWakeup\b", re.IGNORECASE)),
    ("cron-create", re.compile(r"\bCronCreate\b", re.IGNORECASE)),
)

# Anything a keeper can read after the session is gone. ⚠️ A watcher's own timeout
# (`times out ~18:40`) is NOT a clock observable — it dies with the watcher — so a bare
# HH:MM never counts; only a `defer_date` or an ISO date does.
DURABLE = (
    ("file", re.compile(
        r"(?:^|[\s`(\"'])(?:~|\.{0,2})/[\w.-]+|\b[\w-]+\.(?:md|jsonl?|log|ya?ml|txt|out|csv)\b"
        r"|\b(?:files?|mtime)\b", re.IGNORECASE)),
    ("card-id", re.compile(r"\b[0-9a-f]{32}\b", re.IGNORECASE)),
    ("clock", re.compile(r"\bdefer_date\b|\b\d{4}-\d{2}-\d{2}\b")),
    ("external-probe", re.compile(
        r"\bPR\b|#\d+\b|\bCI\b|\bgh\s|\bkubectl\w*\b|https?://|\bpods?\b|\bcontainer\b"
        r"|\brelease\b|\btag\s+v\d|\bworkflow\b|\bdeploy(?:ment)?\b", re.IGNORECASE)),
)


def ends_body(line):
    """Everything after `⏰ Ends:`, entities and backticks stripped."""
    body = line.split(ENDS_MARKER, 1)[1] if ENDS_MARKER in line else line
    return body.replace("&nbsp;", " ").strip().strip("`").strip()


def session_bound_class(line):
    """The session-bound class an `⏰ Ends:` waits on with no durable observable, or `None`."""
    body = ends_body(line)
    if not body:
        return None
    for _name, pattern in DURABLE:
        if pattern.search(body):
            return None
    for name, pattern in SESSION_BOUND:
        if pattern.search(body):
            return name
    return None


def state_dir():
    """The record's directory, read at call time so a test can redirect it."""
    return (os.environ.get(STATE_DIR_ENV)
            or os.path.expanduser(os.path.join("~/.claude/state", STATE_SUBDIR)))


def record_path(session_id, directory=None):
    """The record's path for one session. ⚠️ Fixed and documented: a reader depends on it."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id or UNKNOWN_SESSION)) or UNKNOWN_SESSION
    return os.path.join(directory or state_dir(), f"{safe}.jsonl")


def previous_record(session_id, environ=None):
    """The last record written for this session, or `{}`. Never raises.

    ⚠️ The ONE place this hook's own record is consulted by a verdict, and only by the
    bounded retry below. A missing, empty or unreadable file is a fail-open path like any
    other: the retry is allowed exactly as it was before this existed.
    """
    try:
        directory = (environ or {}).get(STATE_DIR_ENV) or state_dir()
        with open(record_path(session_id, directory), encoding="utf-8",
                  errors="replace") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        return json.loads(lines[-1]) if lines else {}
    except Exception:
        return {}


def trim_records(path, cap=RECORD_CAP_BYTES, keep=RECORD_KEEP):
    """Keep the most recent `keep` lines once `path` passes `cap` bytes. Never raises.

    ⚠️ Written through a temporary file and `os.replace`d, so a kill mid-trim leaves
    either the old file or the new one — never a half-written one.
    """
    try:
        if os.path.getsize(path) <= cap:
            return
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(lines[-keep:])
        os.replace(tmp, path)
    except Exception:
        return


def write_record(record, directory=None):
    """Append one record. ⚠️ Never raises, and never touches the verdict."""
    try:
        os.makedirs(directory or state_dir(), exist_ok=True)
        path = record_path(record.get("session_id"), directory)
        line = json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
        trim_records(path, cap=RECORD_CAP_BYTES, keep=RECORD_KEEP)
        return path
    except Exception:
        return None


def _reap_closer():
    """`scripts/reap-closer.py`, loaded by path.

    ⚠️ Loaded rather than imported: the filename carries a hyphen, and this script ships
    as a plugin artifact with no sibling guaranteed on `sys.path`. `last_closer(path)` is
    the one closer extractor already in this repo — a second copy is the defect, not the
    optimisation.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reap-closer.py")
    spec = importlib.util.spec_from_file_location("reap_closer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def last_closer(transcript_path):
    """`(timestamp, line)` for the transcript's last `👤 You:` line, or `None`.

    Assistant messages only, and the LAST one — both properties belong to
    `reap-closer.py`'s extractor and are pinned by its own tests.
    """
    return _reap_closer().last_closer(transcript_path)


def last_closer_and_ends(transcript_path):
    """`(timestamp, you_line, ends_line_or_None)` from one scan, or `None` with no closer.

    Delegates to `reap-closer.py` like `last_closer` — one scanner, one message.
    """
    return _reap_closer().last_closer_and_ends(transcript_path)


def closer_body(line):
    """Everything after `👤 You: `, with the entities and backticks a closer carries stripped."""
    body = line.split("👤 You:", 1)[1] if "👤 You:" in line else line
    body = body.replace("&nbsp;", " ")
    return body.strip().strip("`").strip()


def match_allow(body):
    """The allow-list entry name the closer's action names, or `None`.

    ⚠️ Matched against the whole closer body, not just the text after the verb: the
    observed `/github-pr-watch` closer spells the verb as `` `watch` `` and names the
    command later in the same line.
    """
    for pattern in DENY:
        if pattern.search(body):
            return None
    for name, pattern in ALLOW:
        if pattern.search(body):
            return name
    for name, pattern in PROSE_ALIASES:
        if pattern.search(body):
            return name
    return None


def _retry_verdict(payload, environ, verdict):
    """The `stop_hook_active` branch: ONE re-block of a re-emitted closer, then allow.

    ⚠️ The loop guard exists so a continued turn ending on the same closer cannot re-block
    forever, and an unconditional allow is not the only way to keep that promise. A model
    that re-emits the closer it was just told to perform gets told once more; a model that
    re-emits it a third time is stuck, and blocking again would loop without end. The bound
    is read from this hook's own record — the file the docstring already promises is
    written on ALLOW and BLOCK alike — so it survives across the separate `Stop`
    invocations, and the worst case is two consecutive blocks, never a loop.

    ⚠️ Every uncertainty allows. A missing transcript, an unreadable record, a different
    closer, or a previous record that was already a retry all fall back to the old
    unconditional allow, so a broken read can only cost the extra block, never a stuck turn.
    """
    inner = dict(payload)
    inner["stop_hook_active"] = False
    code, text, record = evaluate(inner, environ)
    closer = record.get("closer")
    if code != 2:
        return verdict(0, "stop-hook-active", closer=closer)
    previous = previous_record(payload.get("session_id"), environ)
    if not closer or previous.get("decision") != DECISION_BLOCK:
        return verdict(0, "stop-hook-active", closer=closer)
    if previous.get("reason") == RETRY_REASON:
        # ⚠️ The retry already happened once. This is the third identical stop, which is
        # the state the guard exists for — allowing it is what keeps the bound finite.
        return verdict(0, "stop-hook-active", closer=closer)
    if previous.get("closer") != closer:
        return verdict(0, "stop-hook-active", closer=closer)
    code2, _unused, retry = verdict(2, RETRY_REASON, verb=record.get("verb"),
                                    matched=record.get("matched"), closer=closer,
                                    cls=record.get("class"), phrase=record.get("phrase"))
    return code2, _retry_message(text), retry


def _retry_message(text):
    """The re-block's stderr: says it is a retry, then repeats the original reason."""
    return (
        "closer-gate-lint: this closer was blocked on the previous stop and has been "
        "re-emitted unchanged. Blocking once more instead of releasing it through the loop "
        "guard; perform the step now. (A third identical stop is allowed, so this cannot "
        "loop.)\n" + text
    )


def evaluate(payload, environ=None):
    """`(exit_code, stderr_text, record)` — the whole decision, pure but for the transcript read."""
    environ = os.environ if environ is None else environ
    session_id = payload.get("session_id") if isinstance(payload, dict) else None

    def verdict(code, reason, verb=None, matched=None, closer=None, cls=None, phrase=None):
        return code, _message(reason, verb, matched) if code else "", {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "decision": DECISION_BLOCK if code else DECISION_ALLOW,
            "reason": reason,
            "verb": verb,
            "matched": matched,
            "closer": (closer or "")[:200] or None,
            "class": cls,
            "phrase": (phrase or "")[:200] or None,
        }

    if (environ.get(KILL_SWITCH_ENV) or "").strip().lower() == "off":
        return verdict(0, "kill-switch")
    if payload.get("stop_hook_active") is True:
        # ⚠️ A loop guard, not a courtesy — see the module docstring. Bounded, not
        # unconditional: one re-block, then the stop is allowed.
        return _retry_verdict(payload, environ, verdict)

    transcript = payload.get("transcript_path")
    if not transcript:
        return verdict(0, "no-transcript-path")
    try:
        found = last_closer_and_ends(transcript)
    except Exception:
        return verdict(0, "transcript-unreadable")
    if not found:
        return verdict(0, "no-closer")

    _ts, line, ends = found
    # ⚠️ Before the gate checks: the shape this catches ends `👤 You: nothing`, which they
    # return `not-a-gate` on. Fail-open — a matcher error skips this check, never blocks.
    if ends:
        try:
            cls = session_bound_class(ends)
        except Exception:
            cls = None
        if cls:
            code, _text, record = verdict(2, "session-bound-ends", closer=line,
                                          cls=cls, phrase=ends_body(ends))
            return code, _ends_message(cls, ends_body(ends)), record
    body = closer_body(line)
    handle_gate = HANDLE_GATE_RE.match(body)
    if handle_gate:
        bad = unpollable_handles(body)
        if bad:
            code, _text, record = verdict(2, "unpollable-handle",
                                          verb=handle_gate.group(1).lower(), closer=line)
            return code, _handle_message(bad), record
    gate = GATE_RE.match(body)
    if not gate:
        return verdict(0, "not-a-gate", closer=line)
    verb = gate.group(1).lower()
    matched = match_allow(body)
    if matched is None:
        return verdict(0, "not-allow-listed", verb=verb, closer=line)
    return verdict(2, "allow-listed", verb=verb, matched=matched, closer=line)


def _ends_message(cls, phrase):
    return (
        f"closer-gate-lint: `⏰ Ends: {phrase}` waits on a {cls} — a mechanism that runs "
        "inside this session and dies with it, so its notice can never arrive once the "
        "session ends and no keeper can probe it. CLAUDE.md § Async State Closer: the "
        "observable must outlive the session. Rewrite `⏰ Ends:` to lead with something "
        "readable after this session is gone — a file (path or mtime), a full 32-hex card "
        "id, a `defer_date` / ISO date, or an external probe (PR, CI, `gh`, `kubectl`, a "
        "pod) — and keep the watcher in the `(mechanism, id)` slot as the wake. If nothing "
        "durable exists yet, make the producer write one, or set `defer_date` and exit.\n"
    )


def _handle_message(handles):
    named = ", ".join(f"`{h}`" for h in handles)
    return (
        f"closer-gate-lint: the closer names card handle {named}, which is not a full "
        f"{STORE_ITEM_ID_LEN}-hex store item id — `attention-ask.py poll` refuses it, so "
        "nobody can answer this gate through the store. Replace it with the full id from "
        "the post's `ITEM_ID:` line, then end the turn.\n"
    )


def _message(reason, verb, matched):
    if reason != "allow-listed":
        return ""
    # ⚠️ The message must not assert the step is reversible. `gh pr merge` is
    # irreversible, so the old "reversible, in-scope, non-production … keep the line if
    # the action is irreversible" pair contradicted itself on the exact closer the
    # allow-list now releases, and a careful model resolved it by keeping the gate —
    # reinstating the cost the entry exists to remove. Name the precondition instead:
    # it is the thing the rules actually attach, and the thing this hook cannot check.
    lines = [
        f"closer-gate-lint: `{verb}:` names {matched}, which the global rules already "
        "authorise for this step (CLAUDE.md § Execution Phase No Re-Ask; § After Opening "
        "a PR; § Async State Closer).",
    ]
    when = WHEN.get(matched)
    if when:
        lines.append(
            f"Precondition those rules attach, which this hook cannot check: {when}.")
    lines.append(
        "Verify the precondition, then perform the step now and continue — do not end the "
        "turn on an operator gate. Keep the `approve:`/`review:` line only if the "
        "precondition does not hold, or the action is outside this task's declared scope.")
    return "\n".join(lines) + "\n"


def _bare_record(reason):
    """A record for a path that failed before a payload could be parsed.

    ⚠️ `session_id` is `None` here — the stdin document that would have carried it is
    exactly what could not be read — so these land in the `unknown-session` file.
    """
    return {"ts": datetime.now(timezone.utc).isoformat(), "session_id": None,
            "decision": DECISION_ALLOW, "reason": reason,
            "verb": None, "matched": None, "closer": None, "class": None, "phrase": None}


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        write_record(_bare_record("malformed-stdin"))
        return 0
    if not isinstance(payload, dict):
        write_record(_bare_record("non-object"))
        return 0
    try:
        code, text, record = evaluate(payload)
    except Exception:
        # An internal error is a fail-open path like any other, and it is recorded so a
        # guard that is crashing is visible rather than merely quiet.
        write_record(_bare_record("internal-error"))
        return 0
    write_record(record)
    if code:
        sys.stderr.write(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
