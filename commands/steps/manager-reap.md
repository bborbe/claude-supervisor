---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Manager-subject reap

When: every tick — after the open leg

A manager's subject is a goal or a topic page, not a task. The reap contract
(`agents/fleet-drive.md` step 1b) reads three facts from a **task** file, so a manager
bound to a finished subject matches nothing and outlives the work it was started for.
Measured 2026-10-30: a completed goal whose manager session was `LIVE` with its loop
`STOPPED`, idle that way for 31 minutes. Nothing was wrong with the session; the
**binding** was stale.

## 1. Run the scan

⚠️ **This leg acts, and a read-only execution is not a run of it** — it posts the reap card
and hands every reap line to the caller to send. It is one of the round's acting legs
(`commands/fleet-drive.md`, the delegate rule's carve-out), so a sub-agent may run it only
with write tools.

```bash
P=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts
python3 "$P/subject-reap.py" --post-cards
```

It scans every configured vault's `goals_dir` and `topics_dir`, keeps the pages whose
`status` is `completed` or `aborted`, joins each to the manager recorded on it
(`claude_session_id`, falling back to the retired `manager_session_id`), and probes that id
with `session-liveness.py --check`. The three-read order and the fail-closed split live in
that script's header — do not restate them here and do not re-implement the probe.

⚠️ **One reap line per candidate, and `SUMMARY reap=<n>` is the round's reading.** Paste
every `✅ REAP:` line into the payload's `REAPS` block **verbatim**, one row each as
`TO: <subject> [<sid8>] | <line>`, and send each with `SendMessage` to the **full session id
the line names** — a manager's roster name is its subject title, which need not be the page
title, so the id is the address and the title is the label. A summary line is a failure, not
a rendering choice.

## 2. The reap is evidence, never an act

⚠️ **Nothing here kills a process and nothing writes session state.** The session ends
through its own `/supervisor:close`, or `/exit` when it has no task anchor. `subject-reap.py`
imports no `signal`, holds no `os.kill`, and writes no page — the only write the leg makes
is the one `--post-cards` attention card per reaped subject, an `ack` that reports the
condition and asks nothing. A leg that could close the manager itself is a different,
riskier change and is not this one.

## 3. Read the SUMMARY, never the absence of output

| Reading | Means |
|---|---|
| `reap=0` | nothing to do this round — the correct answer for a fleet with no stale binding |
| `reap=<n>` | `<n>` reap lines were emitted and must be sent |
| `UNKNOWN \| …` rows | a probe could not run or answered `AMBIGUOUS` — reported, never acted on |
| `SILENT \| …` rows | `ABSENT` / `MALFORMED` / no id — the reap is moot, and silence is correct |
| exit 2 | a vault root or a goals/topics directory could not be read — **not** a count of 0 |

⚠️ **`reap=0` for two consecutive rounds is not proof the fleet is clean** — the metric
that separates "reaped" from "never looked" is the count itself. If the scan refuses, say
so; a leg that reports `0` because it could not read is the failure this whole leg exists
to make visible.
