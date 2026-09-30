# claude-supervisor

[![CI](https://github.com/bborbe/claude-supervisor/actions/workflows/ci.yml/badge.svg)](https://github.com/bborbe/claude-supervisor/actions/workflows/ci.yml)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/bborbe/claude-supervisor)

Supervise many Claude Code worker sessions from **one** manager session — spawn them, watch them, and **answer their permission prompts**.

The problem it solves: every agent session normally owns a private channel to you (its terminal tab), so you become the multiplexer, and every permission prompt demands a tab switch. Here, workers are sessions the supervisor owns, and their prompts are answered programmatically by the manager — no tab, no code, no `--dangerously-skip-permissions`.

## Install

```
/plugin marketplace add bborbe/claude-supervisor
/plugin install supervisor@claude-supervisor
```

The server runs on **bun** (installs its own node dependencies on first start) and executes on **node**. Both must be on `PATH` in the session that loads the plugin.

## Usage

### Quick start

Spawn a worker, then answer its permission prompt without leaving your manager session:

```
> /supervisor:manager-spawn Run the test suite and fix any failures

Spawned agent_1 (interactive) — pane 1714
```

The worker runs in its own wezterm tab. When it needs approval, the prompt parks for **you** instead of the tab:

```
> /supervisor:manager-answer

1 pending — agent_1
  Bash: go test ./...
  allow / deny ?
```

Answer it and the worker carries on. That is the whole loop: `/supervisor:manager-spawn` reports the `agent_id`, the mode it chose, and — for a tab worker — the `pane_id`; `/supervisor:manager-answer` drains whatever is waiting.

For a worker whose prompts you intend to answer yourself rather than letting its tab handle them, spawn it headless with `interactive=false` — that is the only mode the approval loop serves.

### What you get

| Piece | Job |
|---|---|
| MCP server `supervisor` | spawns and owns the workers; parks their permission prompts |
| skill `supervising-workers` | when to spawn a worker instead of doing the work yourself |
| `/supervisor:manager-spawn` `/supervisor:fleet-workers` `/supervisor:manager-answer` `/supervisor:manager-drain` | the operator surface |
| agent `manager-wrangler` | runs the routine approval loop on a cheap model, escalating only real forks |
| `/supervisor:jump` `/supervisor:who-needs-me` | find the sessions blocked on you, and jump to their pane |
| `/supervisor:attention-next` | what's next on the attention stack — answer an item in chat, delivered to the session that asked |
| `/supervisor:inbox` | the operator's approval queue — every live task at `phase: todo` across every vault, plus the three verbs that move one (approve / reject / later). The command never picks a row; each verb resolves its vault through `scripts/inbox.py --resolve`, which refuses on ambiguity |
| `scripts/jump-link.py` | render a pane's jump target as a clickable link (falls back to `/supervisor:jump <N>` when the local fleet-jump server is not configured) |
| `scripts/restart-worker.py` | restart ONE stale idle worker session under a narrow allow rule — seven registry-guarded refusals, never a manager, never a broad `kill` |
| `scripts/manager-predispatch.py` | the **pre-dispatch gate** `/supervisor:manager-drive` and `/supervisor:manager-status` run first — when the tracked tree has not moved it replays the stored table and dispatches **no agent**; a worker dying counts as movement, because session liveness is in the digest |
| `/supervisor:fleet-loop` `/supervisor:fleet-status` | watch every session on the machine; one stateful loop, one read-only snapshot |
| `/supervisor:fleet-drive` | one-shot by hand, and dispatched by `/supervisor:fleet-loop` every round: nudge idle sessions with open work and no verified blocker; escalate the rest grouped by cause |
| `/supervisor:fleet-verify` | read-only: verify the fleet layer's own contract, ending in a numbered fix list |
| `/supervisor:manager-loop` `/supervisor:manager-status` | watch ONE goal or topic; its task set, its sessions, what is blocked on you |
| `/supervisor:manager-verify` | read-only: verify ONE goal or topic and suggest fixes, ending in a numbered fix list |
| `/supervisor:stop` | stand a manager loop down — disarm the model-waking cadence, keep the gate loop and the session |
| `/supervisor:reset` | re-discover a manager's state from disk — re-resolve the subject, force a full sweep, re-validate the asks ledger without discarding, re-read the tracked set |
| `/supervisor:worker-drive` | a worker drives its own anchored task to done |
| `/supervisor:worker-restart` | restart ONE named worker in one verb — kill, resume, and tell it what changed only when that is safe |
| `/supervisor:open` | resolve a task, goal or topic by name, then jump to its live session, resume its recorded one, or spawn a new one (manager-only; needs `vault-cli`) |
| `/supervisor:ready` | ready ONE named task to the spawn bar — audit → repair → re-audit inside a sub-agent — then open it or escalate naming the gap (cross-tier; needs `vault-cli`) |
| `/supervisor:read-guides` | load every guide in `docs/` — start here to learn which commands your session tier may run |

## Configuration

### Two spawn modes

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
{ "spawn": { "mode": "interactive", "maxConcurrent": "" } }
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

### Setting the fleet-wide concurrent limit

`spawn.maxConcurrent` bounds how many workers the fleet may hold open **at once** — one value, fleet-wide. It replaces the per-manager spawn cap (2 per sweep, 4 per rolling 30 min) that `docs/fleet-surface.md` § Spawn a worker carried until 2026-09-27. Source order matches `mode`: `SUPERVISOR_MAX_CONCURRENT`, then the config file.

⚠️ **It ships empty, and empty means unlimited.** That is the correct shipped state, not a placeholder awaiting a value. The operator's ruling of 2026-09-27, verbatim: *"These limits are artificial and should be removed … It's more a global concurrent limit we should aim than these local limits"* and *"For now there is no global limit."* Nothing here invents a number on the operator's behalf — set one when load demands it, and `0` is accepted as the same answer because that is the value you reach for to turn a limit **off**.

**The count is live workers, not live sessions.** It is read from the heartbeat store the spawner stamps for every worker it opens; the session registry would also count the operator's own sessions and the manager's, which makes a small limit unusable in practice.

An unusable value — a negative number, a fraction, a stray boolean — **refuses every spawn** and names the file, on the same reasoning as the mode refusals above: `Number(true)` is `1`, so a blind coercion would read a stray `true` as "one worker at a time" and never say so. An unreadable heartbeat store also refuses, rather than opening past a limit it cannot count.

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

*Corrected 2026-09-15: this said "the managed settings tier", which sends a reader to a root-owned file they cannot edit. The tier was inferred from key-level provenance — `provenance.permissions` names the highest-precedence contributor for the whole `permissions` object, and the managed drop-in contributes an `allow` entry, so it reported `managed` while `defaultMode` itself came from `user`. A per-source dump settles it: `user -> auto ~/.claude/settings.json`, and the managed drop-in carries no `defaultMode` at all.*

How the server decides which mode that is, and why it errs toward over-reporting: it scans the settings tiers individually rather than trusting the merged value. `project` outranks `user`, so a project-tier `permissions.defaultMode: default` displaces a trusted tier's `auto` in the merge — and the SDK's trust filter, whose job is to drop escalating modes from repo-committed files, inspects only the merged value and its key-level provenance, so the displaced `default` passes through as though nothing had been overridden. Measured 2026-09-16: a live worker under exactly that configuration was auto-approved with no hook call, no `canUseTool` call, and no permission-log line, while the merged value read `default` and the guard called the policy reachable. So any tier the filter does not strip — `user`, `local`, `managed`, `flag` — holding `auto` or `bypassPermissions` is treated as decisive, unranked by precedence among themselves. That can warn about a worker that would have been fine; the alternative is a policy accepted and then silently ignored, which is the failure this section exists to describe.

The same boundary applies the other way: a tool the worker's own settings already allow never reaches the hook either, so a policy can narrow what escalates but cannot revoke an inherited allow.

## Tools

| Tool | Purpose |
|---|---|
| `spawn_agent(prompt, cwd?, label?, interactive?, resume?, policy?)` | start a worker — a real session in a tab by default, or headless with `interactive: false`. `policy` gives this one worker its own rules; headless only |
| `send_agent_message(agent_id, message)` | type a follow-up into a running **tab** worker and submit it |
| `list_agents()` | every worker with status and pending-permission count |
| `agent_status(agent_id)` | one worker: status, last message, **the current tool call and how long it has been held**, result, plus `session_status` / `awaiting_input`. `result.total_cost_usd` appears **only when the worker reached Anthropic itself** — under a router the SDK still prices from Anthropic's list, so the figure would describe a billing model the traffic never touched and it is omitted rather than disclaimed |
| `pending_permissions()` | prompts awaiting an answer, across all workers |
| `await_permission(timeout_ms?)` | block until any worker asks — one call instead of polling |
| `answer_permission(request_id, behavior, message?)` | `allow` / `deny` — this unblocks the worker. ⚠️ Gated by **your own session's** permission mode, not the worker's: under `auto` the classifier can refuse the outgoing call (measured 2026-09-19). Fix with Shift+Tab → `accept edits`, then retry — never by changing the worker's mode |

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

## Documentation

- [Fleet surface](docs/fleet-surface.md) — the spawn shape and the table render spec (canonical)
- [Session tiers](docs/session-tiers.md) — which commands each session tier may run
- [Subject resolution](docs/subject-resolution.md) — the four-source subject chain the manager commands share
- [Pane reads](docs/pane-reads.md) — reading a worker's pane
- [Restart a worker](docs/restart-worker.md) — the kill-and-resume procedure

Architecture, the spawn ledger and the known gaps are documented in [CLAUDE.md](CLAUDE.md) — that file is for agents working in the tree, not for using the plugin.

## Development

```bash
make precommit   # versions, changelog, spawn mode, lint, tests
```

## License

BSD-2-Clause — see [LICENSE](LICENSE).
