# claude-supervisor

Supervise many Claude Code worker sessions from **one** manager session — spawn them, watch them, and **answer their permission prompts**.

The problem it solves: every agent session normally owns a private channel to you (its terminal tab), so you become the multiplexer, and every permission prompt demands a tab switch. Here, workers are sessions the supervisor owns, and their prompts are answered programmatically by the manager — no tab, no code, no `--dangerously-skip-permissions`.

## Install

```
/plugin marketplace add bborbe/claude-supervisor
/plugin install supervisor@claude-supervisor
```

The server runs on **bun** (installs its own node dependencies on first start) and executes on **node**. Both must be on `PATH` in the session that loads the plugin.

## What you get

| Piece | Job |
|---|---|
| MCP server `supervisor` | spawns and owns the workers; parks their permission prompts |
| skill `supervising-workers` | when to spawn a worker instead of doing the work yourself |
| `/supervisor:manager-spawn` `/supervisor:fleet-workers` `/supervisor:manager-answer` `/supervisor:manager-drain` | the operator surface |
| agent `manager-wrangler` | runs the routine approval loop on a cheap model, escalating only real forks |
| `/supervisor:jump` `/supervisor:who-needs-me` | find the sessions blocked on you, and jump to their pane |
| `/supervisor:attention-next` | what's next on the attention stack — answer an item in chat, delivered to the session that asked |
| `scripts/jump-link.py` | render a pane's jump target as a clickable link (falls back to `/supervisor:jump <N>` when the local fleet-jump server is not configured) |
| `/supervisor:fleet-loop` `/supervisor:fleet-status` | watch every session on the machine; one stateful loop, one read-only snapshot |
| `/supervisor:fleet-drive` | one-shot: nudge idle sessions with open work and no verified blocker; escalate the rest grouped by cause |
| `/supervisor:manager-loop` `/supervisor:manager-status` | watch ONE goal or topic; its task set, its sessions, what is blocked on you |
| `/supervisor:stop` | stand a manager loop down — disarm the model-waking cadence, keep the gate loop and the session |
| `/supervisor:reset` | re-discover a manager's state from disk — re-resolve the subject, force a full sweep, re-validate the asks ledger without discarding, re-read the tracked set |
| `/supervisor:worker-drive` | a worker drives its own anchored task to done |
| `/supervisor:open` | resolve a task, goal or topic by name, then jump to its live session, resume its recorded one, or spawn a new one (manager-only; needs `vault-cli`) |
| `/supervisor:read-guides` | load every guide in `docs/` — start here to learn which commands your session tier may run |

## Session tiers

Every command's prefix names the tier of the session that runs it: `fleet-*` for the fleet manager, `manager-*` for a goal/topic manager, `worker-*` for a worker; `jump`, `who-needs-me` and `read-guides` are cross-tier. A worker never opens or spawns sessions (`/supervisor:open` is manager-only) and routes anything out of scope to its manager. The full command → tier table, with "never run from" guidance, is [`docs/session-tiers.md`](docs/session-tiers.md) — the single source; run `/supervisor:read-guides` to load it.

## Two spawn modes

`spawn_agent` opens a worker one of two ways. **They have the same tooling** — same plugin skills, same MCP servers, same launcher-resolved environment (measured: 12 MCP prefixes in both, verified 2026-09-14). The only difference is who answers the worker's permission prompts, and whether you can watch it.

| | `interactive: true` — **default** | `interactive: false` |
|---|---|---|
| What runs | a real `claude` session in a wezterm tab | a `query()` session inside this server |
| Launcher | the `cc-*` script, resolved from `vault-cli config` | same env, via `settingSources` + the launcher's `--mcp-config` |
| Its prompts go to | **its own tab** | **the manager**, via `await_permission` |
| Visible | yes — tab, scrollback, drivable by hand | no tab; tail its transcript instead |
| In `ListAgents` | yes, prefixed `⚙` (it is a real session) | **no** — it has no socket |
| Manager can steer it | yes — `send_agent_message`, or close the tab | **no** — no `interrupt`, and no channel into an SDK session |

**Choose interactive when you want to see or steer it. Choose headless when you want the manager answering its prompts.** Neither is more capable; the trade is visibility against supervision.

### Setting the fleet default

The mode a worker opens in is a **fleet-wide decision**, so it lives in a file you edit rather than in the instructions each manager reads:

```json
// ~/.config/claude-supervisor/config.json   (SUPERVISOR_CONFIG)
{ "spawn": { "mode": "interactive" } }
```

`mode` is `interactive` or `headless`. Four sources, highest first:

| Source | Use it for |
|---|---|
| `spawn_agent({ interactive: … })` | forcing **one** worker against the fleet default — the debug escape hatch, and it works in both directions |
| `SUPERVISOR_SPAWN_MODE` | one manager, one shell, no file edit |
| `spawn.mode` in `config.json` | **the fleet** — one edit, every manager |
| built-in | `interactive` |

The file is read **once at server start**, so restart the MCP server after editing it. It is optional: no file means the built-in default, and that is silent. A file that exists but does not parse is reported, because that is a file you wrote and believe is in effect.

`agent_status`, `list_agents` and each ledger record carry `mode_source` (`argument` / `env` / `config` / `default`), so a worker that opened the wrong way tells you which of the four decided it instead of leaving you to guess.

⚠️ **An unknown `mode` value refuses every spawn**, naming the file and the two valid values — same reasoning as the policy refusals below. A typo that silently fell back to a default would be discovered only by noticing a whole fleet running the wrong way, long after the edit. An unknown *key* only warns, so a config written for a newer version stays usable by this one.

### Notifying the operator when a gate is open

A manager that raises an ACTION gate can only speak to an operator in the room. To reach a phone, add a `notify` block to the same file:

```json
{
  "spawn": { "mode": "interactive" },
  "notify": {
    "env": "dev",
    "endpoints": {
      "dev": { "baseUrl": "https://…", "teamvaultKey": "…" },
      "prod": { "baseUrl": "https://…", "teamvaultKey": "…" }
    }
  }
}
```

`scripts/notify-gate.py --layer <name>` reads the sweep's gates on stdin, publishes each one that is due through the endpoint named by `env`, and keeps a cadence ledger at `~/.claude/state/gate-notifications-<layer>.json` (`SUPERVISOR_GATE_STATE` overrides the path). It also owns cross-layer de-dup, now carried on the attention store item's `escalated_by` rather than a local file: each gate names the BLOCKED session in a `session` field, the script resolves that session's item by `producer_id`, and a gate with no resolvable item is reported as `unresolved` instead of being silently skipped. The script owns the delivery bound — read its docstring rather than copying the numbers.

**`--layer` is required, and the ledger is per layer on purpose.** Each manager layer sees a different slice of the gates — the fleet manager drops every gate a live manager owns — so a shared ledger would let one layer's sweep prune the other's gates as "cleared", after which they would be seen as new and re-notified on every tick.

`teamvaultKey` is a **reference** into TeamVault, not the secret itself: the script fetches the credential with `teamvault-cli` and hands it to curl on stdin, so it never appears in the process table.

`type` is optional and defaults to `pending-approval` — the type the notification core routes to the phone.

⚠️ **An absent or incomplete `notify` block exits non-zero the moment a gate is due**, carrying the fix in its message. It deliberately does not fall back to silence: a gate that never reached the phone and a clean sweep look identical from the manager's side, and only one of them is fine.

## The approval policy

Rules decide what a worker may do **without waking the manager**. Anything the rules do not cover defers to `canUseTool`, which parks it for the manager — that fall-through *is* the escalation path.

Rules come from two files, yours first: `~/.config/claude-supervisor/policy.json` (`SUPERVISOR_POLICY`) overlays the shipped `<plugin>/server/policy.json`. Overlay, not replace — growing the list means appending, never copying. The shipped defaults allow Read/Glob/Grep and Write/Edit inside the worker's cwd, deny `Bash rm -rf`, and escalate everything else. Every request is logged as JSONL (`SUPERVISOR_PERMISSION_LOG`), which is what to promote into rules.

### Allowing a Bash command

⚠️ **Never write a Bash `allow` without `"matchType": "command"`.** A plain `match` is a substring test, and your overlay is evaluated before the bundled rules — so `{"tool": "Bash", "match": "ls ", "action": "allow"}` also matches `rm -rf ~/Documents && ls `, and un-denies it. Any allowed substring can be appended to any command.

```json
{ "tool": "Bash", "match": "ls", "matchType": "command", "action": "allow" }
```

Under `"matchType": "command"`, `match` is a **whole-token prefix** of the command: `ls` matches `ls -la /tmp` but not `lsof`, and a two-token prefix such as `docker ps` matches `docker ps -a` but not `docker rm`. Four shapes never match at all, and so fall through to the bundled rules:

- anything with a shell metacharacter — `;` `&` `|` `` ` `` `<` `>` newline, parens, braces — so nothing can ride along;
- a leading `VAR=value`, because the environment picks the program: `PATH=/tmp/evil ls` and `LD_PRELOAD=… ls` both run attacker code under a genuine `ls`;
- whitespace the shell does not split on (U+00A0, other Unicode spaces, `\r`), so this matcher and bash always agree on which program runs;
- an empty prefix.

**Only the prefix is anchored.** Tokens after it are unconstrained, so allow a prefix only when *every* extension of it is read-only. `ls` qualifies. `sed -n` does not (`sed -n -i`), nor `find` (`-delete`).

⚠️ **No `git` prefix qualifies — not even `git status`.** Git runs commands named in the repository's own config, and the shipped defaults let a worker edit any file in its cwd, `.git/config` included. So a worker can set `core.fsmonitor` to a command of its choosing and then run an allowed `git status`, which executes it with no prompt (verified: `git status` runs `core.fsmonitor`). `core.pager`, `diff.external` and hooks are further routes. The same holds for any tool that reads config or plugins from the working tree.

When in doubt, leave it escalating — a false refusal costs one prompt, a false allow costs the filesystem.

Under this mode `"match": "*"` means *any single uncompounded command*, not *anything*. A rule without `matchType` keeps the substring behaviour, so existing rules and the bundled `deny rm -rf` are unaffected.

This mode needs a server that ships it. An older server ignores `matchType` and reads the rule as a substring — the bypass above — so upgrade the plugin **before** adding such a rule, and confirm with `claude plugin list`.

### A policy for one worker

`spawn_agent({ policy: "<path>" })` gives **one** worker its own rules, evaluated ahead of both files above. An absolute path is used as-is; a relative one resolves against the worker's cwd.

It overlays rather than replaces, so a permissive override does not silently drop the `rm -rf` deny along with it. Full replacement stays reachable: end the file with a `{"tool":"*","match":"*","action":"escalate"}` catch-all, which then matches before the bundled rules get a turn.

Three refusals, each an argument that would otherwise be accepted and then ignored:

- **`interactive: true` + `policy`** — a tab worker answers its own prompts in its tab, so the server never sees them. Pass `interactive: false`.
- **a mode that bypasses the hook** (below) — no policy can take effect. Omit `policy` to spawn anyway under that mode's own rules.
- **a policy file that cannot be read** — a typo or a missing mount must not quietly become "no override".

### The policy only sees what reaches the hook

⚠️ **`auto` and `bypassPermissions` make the whole policy layer inert.** Both answer tool calls without consulting the `PermissionRequest` hook, so no rule — bundled, user, or per-spawn — can take effect. The server warns at startup and refuses a per-spawn policy under either mode.

Measured 2026-09-15 on a machine whose `permissions.defaultMode` resolved to `auto` from **`~/.claude/settings.json`** — the user tier: a headless worker ran a non-allowlisted command, reported `success`, and produced **no hook call, no `canUseTool` call, and no permission-log line at all**. The policy code was correct, unit-tested, and doing nothing. An escalating mode from a trusted settings tier wins over the `permissionMode` query option, so it cannot be fixed from the spawn — set `permissions.defaultMode` to `default` in the settings file that supplies it, which here is your own `~/.claude/settings.json` and not a machine policy you would have to ask someone else to change.

*Corrected 2026-09-15: this said "the managed settings tier", which sends a reader to a root-owned file they cannot edit. The tier was inferred from key-level provenance — `provenance.permissions` names the highest-precedence contributor for the whole `permissions` object, and the managed drop-in contributes an `allow` entry, so it reported `managed` while `defaultMode` itself came from `user`. A per-source dump settles it: `user -> auto /Users/bborbe/.claude/settings.json`, and the managed drop-in carries no `defaultMode` at all.*

How the server decides which mode that is, and why it errs toward over-reporting: it scans the settings tiers individually rather than trusting the merged value. `project` outranks `user`, so a project-tier `permissions.defaultMode: default` displaces a trusted tier's `auto` in the merge — and the SDK's trust filter, whose job is to drop escalating modes from repo-committed files, inspects only the merged value and its key-level provenance, so the displaced `default` passes through as though nothing had been overridden. Measured 2026-09-16: a live worker under exactly that configuration was auto-approved with no hook call, no `canUseTool` call, and no permission-log line, while the merged value read `default` and the guard called the policy reachable. So any tier the filter does not strip — `user`, `local`, `managed`, `flag` — holding `auto` or `bypassPermissions` is treated as decisive, unranked by precedence among themselves. That can warn about a worker that would have been fine; the alternative is a policy accepted and then silently ignored, which is the failure this section exists to describe.

The same boundary applies the other way: a tool the worker's own settings already allow never reaches the hook either, so a policy can narrow what escalates but cannot revoke an inherited allow.

## Reaching a running tab worker

`send_agent_message(agent_id, message)` types a follow-up into a running tab worker and submits it. This is the `send_to_agent` the server never had: a worker that has gone wrong can be corrected, and one that has stalled can be nudged, without a human at the tab.

Three things about it are measured rather than assumed, and each one is a trap:

- **It only works on a tab worker.** A headless worker has no pane, so the call is refused rather than attempted — typing into a pane that does not exist is how a channel reports success while delivering nothing.
- **It activates the worker's tab, which steals focus.** Not silent by design; there is no way to type into a wezterm pane without making its tab active.
- **It waits for the input prompt, it does not sleep.** A fixed delay is a race that fails on a loaded machine. Readiness is the `❯` glyph in the pane, which appears about a second after spawn.

**The colour is set the same way, and that is a bug fix.** `SUPERVISOR_WORKER_COLOR` (default `/color pink`) used to be seeded as the first line of the worker's prompt, and it never worked: Claude Code parses one submitted message as one command, and `/color` takes the *entire* trimmed argument, so the colour swallowed the blank line and the whole task — `Invalid color "pink\n\n<task>"`. It is now sent as its own message once the pane is up, and the spawn response reports `color: {applied: true}` only when it actually landed.

⚠️ **`transcript_dir` on a tab worker is unreliable, and known to be.** It is derived from the `cwd` you passed, but the `cc-*` launcher does `cd` into its own vault — so a worker spawned with `cwd: "/tmp"` runs in `~/Documents/Obsidian/Personal` and writes its transcript there, while the spawn response still says `/tmp`. Measured 2026-09-14.

**Resolved 2026-09-19 for reading a worker's state.** `agent_status` resolves a worker's transcript by **session id** — `<projectsDir>/*/<session-id>.jsonl` — and never from a cwd, so the derivation above is not on that path and is not trusted there. It still describes where the spawn response *claims* the transcript is, and is still wrong for a launcher-`cd` worker: nothing should read it as a location.

### Reading a worker

`agent_status(agent_id)` reports four fields that answer "what is it doing", none of which the write half could tell you:

| field | source | means |
|---|---|---|
| `last_message` | the worker's transcript JSONL | the last thing it said. Not "the last record" — most assistant records carry a `tool_use` block and no text (measured on a live session: 127 assistant records, 27 with text), so this is the last one carrying text. |
| `current_tool_call` | the worker's transcript JSONL | the call it is inside **right now** — `{name, input_summary, started_at, held_seconds}` — or `null` when nothing is in flight. This is the field that separates a worker executing a long tool call from one parked on a gate: `session_status` says both are not-idle, and this says *which* call and *for how long*. `input_summary` is capped at 200 chars and whitespace-flattened; the full input is `pending_permissions`' job for a parked call. `started_at`/`held_seconds` are `null` together when the record carries no timestamp — `name` is still reported, because "which tool" and "for how long" fail independently. |
| `session_status` | the session registry | the raw registry status: `idle`, `busy`, `waiting` or `shell`. |
| `awaiting_input` | derived from the status above | `true` only when the status is `waiting`. **`null`, not `false`, when the registry does not list the session** — unlisted is a different fact from not-waiting, and reporting it as `false` would be a guess wearing a measurement's clothes. |

⚠️ **`awaiting_input` means "blocked on input", not specifically "a permission gate is open".** It is the superset; whether a permission prompt specifically produces `waiting` is what the live A/B pins.

⚠️ **`current_tool_call` names the pending call; it does not say whether the worker is parked.** A pending `tool_use` with no matching `tool_result` reads identically whether the worker is executing a tool or parked on a prompt — measured 2026-09-19 across 25 live sessions, 3 `busy` sessions carried exactly that pending call, indistinguishable by transcript from the 2 `waiting` ones. The two fields are complements: the transcript answers *which call*, the registry answers *is it moving*.

**In flight means the LAST `tool_use` with no `tool_result` carrying its id — not "any unmatched `tool_use`".** An interrupted call leaves an unmatched `tool_use` behind and the conversation carries on, so a scan that reports any of them keeps naming a call the worker abandoned; measured 2026-09-20 over 1 667 live transcripts, 58 carried an unmatched `tool_use` and only 36 had it as their last one. The record's own `timestamp` is the start, so `held_seconds` is measured from the transcript and does not reset when a manager reconnects or polls again.

The read is bounded: a transcript is read from its **tail** (256 KB). The last message is at the end by definition, and `list_agents` renders every worker — reading each file whole would turn one status call into a read of the fleet's entire history. A window that finds nothing reports `null` rather than falling back to a full read.

**That bound costs the tool-call field a blind spot the message field does not have, and it is named rather than hidden.** A `tool_result` always follows its `tool_use`, so a call found inside the window cannot have its result outside it — but a call **older than the whole window** (256 KB of records written after it while it runs) is invisible, and reports as `null`, i.e. as "no call in flight". The message read falls back to a full read on a miss; this one deliberately does not, because a miss there is common (an idle worker has no call to find) and an unbounded read on the status path is the cost the window exists to avoid.

⚠️ **Two scope limits are unchanged by this read, and are named here rather than left to be discovered.** `agent_status` resolves by `agent_id` only — a label returns `unknown agent <label>` — and it reads an in-memory Map inside the caller's own server process, so it only knows workers **that session** spawned. Peers spawned elsewhere are invisible to it entirely, and an empty result is never evidence of absence. The new fields inherit both limits; they do not widen them.

## The spawn ledger

Every worker this server spawns gets a record at `~/.local/state/claude-supervisor/sessions/<uuid>.json`, keyed by its session uuid and written at spawn.

Two stores already exist and neither answers the question this one does:

| store | keyed by | lifetime | says |
|---|---|---|---|
| `~/.claude/sessions/<pid>.json` | pid | **deleted when the session exits** | who is running *right now* |
| `~/.claude/projects/<cwd>/<uuid>.jsonl` | uuid | permanent | the conversation |
| **the ledger** | uuid | permanent | **who started it, in what mode, from which manager, and how it ended** |

Measured 2026-09-14: the live registry held 13 entries against 13 live processes with **zero stale** — it tracks liveness, not history, so it forgets a session exactly when a record would first be useful.

```json
{
  "session_id": "b26cb46e-…", "agent_id": "agent_1", "label": "ledger-drill-tab",
  "mode": "interactive", "launcher": "…/cc-personal-deepseek", "pane_id": "1714",
  "resumed_from": null, "policy": null, "parent_session": "0096a027-…",
  "spawned_at": "2026-09-14T06:10:31.153Z", "ended_at": null,
  "status": "running", "result": null
}
```

`parent_session` is the **spawn edge** — the manager session that called `spawn_agent`, resolved once by walking up from this server's own pid to the **nearest ancestor the live registry knows**. It is *not* the direct parent pid: `.mcp.json` starts this server through a `bun run` wrapper, so the direct parent is that wrapper and a bare `process.ppid` lookup named nothing — which is why this field was `null` in every record written before the walk existed. When no ancestor is registered — an exited manager, or a chain that never passed through a session — the field stays `null` rather than carrying a guess. Nothing else records it. A worker whose session id never resolved gets no record rather than one filed under a key nothing would look up, and the server logs that rather than staying quiet.

**Why the id may never resolve.** For a tab worker the id is found *by name* — `findRegisteredByName("⚙ " + label)`, an exact match, polled for up to 8s — so the name is the join back to the session. A name is not unique, and `find` returns the first holder, so a label reused while an earlier worker still answered to it resolved the new spawn to **that** worker and filed the old session's id as the new one's. The spawn therefore derives a free name before starting the process (`⚙ <label> (2)`, … until nothing holds it) and refuses poll matches against holders that existed before the spawn, so the join can only resolve to the session it created. A suffixed tab title is that guard working. See `docs/fleet-surface.md` § *The tab name is the join*.

⚠️ **This is not a liveness source.** An entry here must never be read as proof a session is alive — `liveness.mjs` owns that question, and it answers from the session registry plus the server's in-process record of workers it spawned. The ledger is deliberately the durable half.

`SUPERVISOR_LEDGER_DIR` overrides the location. It is deliberately *not* `SUPERVISOR_SESSIONS_DIR`, which already means the live registry — one variable meaning two stores is how a reader ends up pointing this one at Claude Code's directory.

⚠️ **A headless worker is invisible to `ListAgents` and `/fleet-status`.** A roster-only sweep will declare its task unowned and may spawn a duplicate onto it. Check `list_agents` before concluding a task has no owner.

⚠️ **`permissionMode: 'auto'` bypasses supervision entirely** — the hook never fires and `canUseTool` is never called. Measured: the same `kubectl create secret` that parked for approval under `'default'` ran with **zero** permission requests under `'auto'`. Leave it unset.

## Adopting an existing session

The supervisor can only supervise sessions **it created** — `canUseTool` is attached when a session starts and cannot be grafted onto a running process. That makes its control forward-looking by default: sessions started elsewhere are observable and messageable, but their prompts stay theirs.

`resume` inverts that. Pass a session id and the supervisor opens a **new** session continuing that conversation — and because *this* call creates it, the permission callback applies.

```
spawn_agent({ prompt, resume: "<session-id>", interactive: false })
  → continues the old conversation
  → its prompts park for the manager
```

**Verified 2026-09-14:** a session killed hours earlier was resumed, recalled its own prior task unprompted, parked a `Write` prompt for the manager, and completed on approval. Same session id — resume continues rather than forks.

**Why this matters:** it reaches what the permission *channel* exists for, with no worker-side plugin, no `--channels` flag and no marketplace dependency. The channel was ruled out as a Non-goal precisely to avoid those.

⚠️ **The session must be closed, and a live one is refused.** Two writers on one conversation corrupt it, so `spawn_agent` probes before resuming and returns an error rather than opening the session: a session found running is refused, and so is one whose liveness cannot be determined. Two channels, because neither is enough alone — the session registry at `~/.claude/sessions/<pid>.json`, which is the only one that finds a session started *fresh* and whose entry is **deleted when the session exits** (the property that makes its absence mean something), and the server's own in-process record of the workers it spawned, which is the only one that can see a **headless** worker: that worker is an in-process SDK `query()` with no pid and no argv, so no process listing can find it. `SUPERVISOR_SESSIONS_DIR` overrides the registry location.

⚠️ **A process listing is not a liveness source for this question, and an argv probe used to be here.** `pgrep -fl <id>` matched the full command line of any process, so a finished worker whose id was merely *mentioned* — by a shell, a watcher, a grep — read as live and became unresumable. It is gone. The reason is specific rather than "argv is unreliable": a resumed interactive session *does* carry its id in argv and `pgrep` would find it, but that is a true positive for a different question. This guard asks whether a headless worker is live, and argv cannot answer that. Do not reintroduce it.

**Which conversation am I in?** `agent_status` reports `resumed_from` and `continued` — the latter `true` when the session id came back the same (continued) and `false` when it did not (forked), so an adoption is never mistaken for a fresh start.

⚠️ **Unmeasured:** `forkSession` and `resumeSessionAt`. Supported by the SDK; their interaction with `resume` has not been tested, and no run has yet produced a `continued: false`.

## Tools

| Tool | Purpose |
|---|---|
| `spawn_agent(prompt, cwd?, label?, interactive?, resume?, policy?)` | start a worker — a real session in a tab by default, or headless with `interactive: false`. `policy` gives this one worker its own rules; headless only |
| `send_agent_message(agent_id, message)` | type a follow-up into a running **tab** worker and submit it |
| `list_agents()` | every worker with status and pending-permission count |
| `agent_status(agent_id)` | one worker: status, last message, **the current tool call and how long it has been held**, result, plus `session_status` / `awaiting_input` (see [Reading a worker](#reading-a-worker)). `result.total_cost_usd` appears **only when the worker reached Anthropic itself** — under a router the SDK still prices from Anthropic's list, so the figure would describe a billing model the traffic never touched and it is omitted rather than disclaimed |
| `pending_permissions()` | prompts awaiting an answer, across all workers |
| `await_permission(timeout_ms?)` | block until any worker asks — one call instead of polling |
| `answer_permission(request_id, behavior, message?)` | `allow` / `deny` — this unblocks the worker. ⚠️ Gated by **your own session's** permission mode, not the worker's: under `auto` the classifier can refuse the outgoing call (measured 2026-09-19). Fix with Shift+Tab → `accept edits`, then retry — never by changing the worker's mode |

## How it works

Each worker is a `query()` session from `@anthropic-ai/claude-agent-sdk`, started with `permissionMode: 'default'`, so every approval-requiring tool reaches the server: the `PermissionRequest` hook answers what the policy knows, and anything it does not cover falls through to `canUseTool`, which parks the request for the manager to resolve through `answer_permission`.

⚠️ `permissionMode: 'default'` is a **request, not a guarantee**. An escalating `permissions.defaultMode` from a trusted settings tier wins over it and bypasses the hook and `canUseTool` both, which makes the policy inert — see § The approval policy.

⚠️ **The reverse direction has its own gate.** The manager's *outgoing* `answer_permission` call is itself subject to the **manager session's** permission mode. Under `auto` the classifier can refuse that call before the fleet is involved — the refusal reads like a worker-side denial but is not one (measured 2026-09-19, corrected here from an earlier reading that called it a structural gate). The fix is in the manager session: Shift+Tab → `accept edits`, then retry the same call.

```
manager session ──MCP──► supervisor server ──query()×N──► workers
                              ▲                              │
                              └──── canUseTool ──────────────┘
```

**Escalation chain:** worker → policy → `manager-wrangler` → manager → human. Routine approvals should never reach the manager, and genuine forks should never reach the human.

## Layout

```
.claude-plugin/marketplace.json          marketplace manifest
.claude-plugin/plugin.json               plugin manifest
.mcp.json                                starts the server (${CLAUDE_PLUGIN_ROOT}/server)
skills/supervising-workers/SKILL.md
skills/open-items/SKILL.md               the operator-asks ledger — rules + /supervisor:open-items (script stays at scripts/open-items.py)
commands/{spawn,workers,answer,drain}.md
commands/{jump,who-needs-me}.md                  find and reach a session
commands/attention-next.md + scripts/attention-answer.py  answer an attention item, route it to the asker
commands/{fleet-loop,fleet-status,fleet-drive}.md  fleet surface — many sessions
commands/{manager-loop,manager-status}.md       one goal or topic
commands/manager-drive.md                        one goal or topic — the act leg, by hand
commands/worker-drive.md                         one worker session — drive its anchored task to done
commands/open.md                                 resolve a name → jump / resume / spawn (manager-only)
commands/stop.md                                 stand that loop down
commands/reset.md + scripts/reset.py             re-discover its state; never deletes the ledger
docs/fleet-surface.md                            spawn shape + table render spec (canonical)
docs/session-tiers.md                            session tiers + command → tier table (canonical)
commands/read-guides.md                          load every guide in docs/
scripts/{jump,who-needs-me}.py                   their helpers
agents/manager-wrangler.md                routine approval loop over headless workers
agents/manager-sweep-reader.md            the worker sweep's read-only half (called by both worker commands)
agents/fleet-sweep-reader.md             the fleet sweep's read half, Steps 0b–3 (called by /fleet-loop, read-only by /fleet-drive)
agents/manager-drive.md                    the worker sweep's act leg (composed by manager-loop, runnable by hand)
agents/fleet-drive.md                    the fleet drive leg — revive/blocked split, re-nudge ledger (called by /fleet-drive)
server/supervisor.mjs                    the MCP server
server/policy.json                       bundled approval rules (see § The approval policy)
```

## Status

Extracted from a working prototype proven end-to-end on 2026-09-13: one manager session spawned two workers concurrently, answered both `Write` prompts, and both workers completed (`permission_denials: 0`).

Known gaps, tracked rather than hidden:

- **A headless worker cannot be *steered* mid-task by the supervisor tool — but it is not unreachable.** `send_agent_message` reaches a *pane*, so it does not apply here, and an SDK string-prompt session is single-shot; multi-turn needs streaming input (`AsyncIterable<SDKUserMessage>`). ⚠️ **Read that as a limit on the *tool*, never on the worker:** ordinary cross-session `SendMessage` reaches a headless worker mid-task, in both directions (measured 2026-09-20), and a worker whose question is parked is answered over the permission channel. **A headless worker that has *exited*** — turn end, or a ~11 min question timeout — is **continued, not restarted**; see § Spawn a worker in [`docs/fleet-surface.md`](docs/fleet-surface.md) for the shape and its two traps. A tab worker can be steered with `send_agent_message` or stopped by closing its tab.
- **The policy layer is inert under `auto` / `bypassPermissions`** — both answer tool calls without consulting the `PermissionRequest` hook, so no rule can take effect. The server warns at startup and refuses a per-spawn policy under either. Measured 2026-09-15 on a machine whose `defaultMode` resolved to `auto` from `~/.claude/settings.json`. See § The approval policy.
- **Status lags after an allow** — `agent_status` can still read `running` for a few seconds, because the server marks a worker running the moment it *answers* the prompt, not when the SDK confirms it resumed. That is the most the server can honestly know at that point, so the fix is to stop treating one post-allow check as a verdict: `answer_permission` already returns the outcome, and a worker that was about to finish will read `done` a moment later.
- **Unanswered prompts auto-deny** after 15 minutes — headless workers only; a tab worker's prompt waits for its tab.
