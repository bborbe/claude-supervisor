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

⚠️ **`stop_hook_active` is a loop guard, not a courtesy.** Exit 2 continues the turn; a
model that ends the continued turn on the same closer would re-block forever without it.
When the harness says a Stop hook is already active, this allows the stop unconditionally.

Usage (from `hooks/hooks.json`, `Stop`):

    stdin: the hook payload, carrying `transcript_path` and `stop_hook_active`
    exit 0: allow the stop (also every fail-open path)
    exit 2: block the stop; stderr is shown to the model, which continues the turn

⚠️ **The record is written on ALLOW and BLOCK alike**, so an absent record means the hook
did not run rather than that nothing happened. The write is fail-open and never changes
the verdict: a lost record is preferred to a wrong verdict.

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

# `approve:` / `review:` are the two gate verbs the closer contract defines. `you run:` is
# deliberately not here — a command the operator must execute is not a step the model may
# simply perform.
GATE_RE = re.compile(r"^(approve|review):\s*(.+)$", re.IGNORECASE)

# Derived from `~/.claude/CLAUDE.md` § After Opening a PR and § Execution Phase No Re-Ask
# § Named No-Ask Actions. ⚠️ Order is irrelevant; the name is what the record carries.
ALLOW = (
    ("github-pr-watch", re.compile(r"/github-pr-watch\b", re.IGNORECASE)),
    ("coding-local-review", re.compile(r"/coding:local-review\b", re.IGNORECASE)),
    ("coding-pr-review", re.compile(r"/coding:pr-review\b", re.IGNORECASE)),
    ("gh-pr-ready", re.compile(r"\bgh\s+pr\s+ready\b", re.IGNORECASE)),
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


def state_dir():
    """The record's directory, read at call time so a test can redirect it."""
    return (os.environ.get(STATE_DIR_ENV)
            or os.path.expanduser(os.path.join("~/.claude/state", STATE_SUBDIR)))


def record_path(session_id, directory=None):
    """The record's path for one session. ⚠️ Fixed and documented: a reader depends on it."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id or UNKNOWN_SESSION)) or UNKNOWN_SESSION
    return os.path.join(directory or state_dir(), f"{safe}.jsonl")


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


def evaluate(payload, environ=None):
    """`(exit_code, stderr_text, record)` — the whole decision, pure but for the transcript read."""
    environ = os.environ if environ is None else environ
    session_id = payload.get("session_id") if isinstance(payload, dict) else None

    def verdict(code, reason, verb=None, matched=None, closer=None):
        return code, _message(reason, verb, matched) if code else "", {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "decision": DECISION_BLOCK if code else DECISION_ALLOW,
            "reason": reason,
            "verb": verb,
            "matched": matched,
            "closer": (closer or "")[:200] or None,
        }

    if (environ.get(KILL_SWITCH_ENV) or "").strip().lower() == "off":
        return verdict(0, "kill-switch")
    if payload.get("stop_hook_active") is True:
        # ⚠️ A loop guard, not a courtesy — see the module docstring.
        return verdict(0, "stop-hook-active")

    transcript = payload.get("transcript_path")
    if not transcript:
        return verdict(0, "no-transcript-path")
    try:
        found = last_closer(transcript)
    except Exception:
        return verdict(0, "transcript-unreadable")
    if not found:
        return verdict(0, "no-closer")

    _ts, line = found
    body = closer_body(line)
    gate = GATE_RE.match(body)
    if not gate:
        return verdict(0, "not-a-gate", closer=line)
    verb = gate.group(1).lower()
    matched = match_allow(body)
    if matched is None:
        return verdict(0, "not-allow-listed", verb=verb, closer=line)
    return verdict(2, "allow-listed", verb=verb, matched=matched, closer=line)


def _message(reason, verb, matched):
    if reason != "allow-listed":
        return ""
    return (
        f"closer-gate-lint: `{verb}:` names {matched}, which the global rules already "
        "authorise for a reversible, in-scope, non-production step "
        "(CLAUDE.md § Execution Phase No Re-Ask; § After Opening a PR).\n"
        "Do not end the turn on an operator gate — perform the step now and continue. "
        "Keep the `approve:`/`review:` line only if the action is production-touching, "
        "irreversible, or outside this task's declared scope.\n"
    )


def _bare_record(reason):
    """A record for a path that failed before a payload could be parsed.

    ⚠️ `session_id` is `None` here — the stdin document that would have carried it is
    exactly what could not be read — so these land in the `unknown-session` file.
    """
    return {"ts": datetime.now(timezone.utc).isoformat(), "session_id": None,
            "decision": DECISION_ALLOW, "reason": reason,
            "verb": None, "matched": None, "closer": None}


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
