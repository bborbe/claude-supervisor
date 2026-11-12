# Role × mode decision matrix

The single home for how a task's `role:` and `mode:` frontmatter are decided — at **creation** (`vault-cli` `create-task` and its `task-creator` agent) and at **open** (`/supervisor:open` § Step 0.6, for a row that predates creation-time writes). Both sides cite this file by path; neither restates it.

Operator ruling, 2026-11-06: *"we should have somewhere a decision matrix ... for create task ... we can use this for open task that miss these too"* — and, on the batch: *"if i say 'open all flagged' we should open all ... better open wrong ... than dont open at all"*.

## Rule 1 — `role`

Ask of the task **body** (never the title or frontmatter): *does finishing this task need the operator's own hands, their presence, or a judgement only they can make?*

| Body carries | `role` |
|---|---|
| the physical world — a tank to drain, a machine to power off, a device to hold | `human` |
| a GUI-only surface with no agent interface | `human` |
| the operator's own approval IS the work — a live trade, a sign-off, a decision reserved to them | `human` |
| anything else, including `kubectl`, `ssh`, a deploy | `agent` |
| **no signal at all** — an empty or stub body | **`agent` (default)** |

`manager` is never derived. It is declared by whoever files the row, and only that.

**A missing or undecidable `role` is a default, never a refusal.** An operator flag is the approval; a wrong default is visible on disk and corrected with one `vault-cli task set` line, while a refusal costs the operator a manual open.

## Rule 2 — `mode`

| `role` | `mode` |
|---|---|
| `human` | `interactive` (forced) |
| `manager` | `interactive` (forced) |
| `agent` | **`headless` (forced)** |

**`role: agent` is `headless` always — a task body's signals no longer select `interactive`.** Operator ruling, 2026-11-12: *"all agents should be headless … only interactive and manager should be tabs"*. An agent row never opens a tab, whatever its body carries and whatever its `mode:` field says on disk; a stale `mode: interactive` is rewritten to `headless` and the correction printed, so the next run reads the corrected value rather than re-deriving it.

⚠️ **This supersedes the 2026-11-07 narrowing**, which read the other way: it kept an `agent` row `interactive` when its body carried one of the five watch-need signals, and forced `headless` only for a signal-free body. The five signals are still documented in [`docs/fleet-surface.md` § Spawn a worker](fleet-surface.md#spawn-a-worker) item 6 as *what a task body can carry* — they are no longer a mode input anywhere.

**Why the strict form, measured 2026-11-12:** over 24 h, **24 of the 90 `role: agent` new-worker spawns opened as tabs**, every one recording `mode_source=argument` — the spawn sites did decide, and decided `interactive`. The signals were doing the selecting, and nearly every infrastructure task carries one (an approval gate, an "ask if unsure" step, a judgement reserved to the operator), so the classifier could not separate a task that needs watching from one that merely mentions a gate. Two roles need a screen — the operator and a manager — and that is the whole set.

## Answer-path remedy

`/supervisor:open` probes a **headless** row's answer path before the spawn (`scripts/answer-path-check.py`; exit 1 → refuse the row, print the probe's reason, open nothing). The probe carries the reason only — the remedy a spawn site adds is **role-aware**, because Rule 2 makes the two roles' permitted outcomes opposite:

| `role` | Remedy |
|---|---|
| `agent` | *Arm this session's manager loop and re-run the spawn*; where no loop can be armed (no `worker-manager` registration), the row stays **refused** for the tick. **Never** *open interactive* — that is the tab Rule 2 forbids, and it is the cheaper remedy, so a refused spawner takes it (measured 2026-11-12, the same count Rule 2 records: **24 of the 90** `agent` new-worker spawns opened as tabs in 24 h, every one `mode_source=argument`). |
| `human` / `manager` | Open the row `interactive`, or arm this session's manager loop and re-run — a tab is what the role is for. |

⚠️ **The bound supervisor does not enforce this probe.** The live daemon is the Go `claude-wrapper` at `127.0.0.1:18090/supervisor/mcp`, and its spawn path carries no answer-path guard; the refusal belongs to the command, so a session that skips the step is not caught downstream.

## Rule 3 — `phase`

A missing or empty `phase` is not decided here: it takes the existing entry path (`vault-cli task work-on` advances it; on the `--flagged` batch the operator's flag is the approval).

## Rule 4 — how a `role: human` row opens

- Always opened, into the **Direct** window, as the operator's own session — never skipped, never as a worker.
- On the vault's **Claude launcher** (the `-claude` sibling of `claude_script` when it exists, else `manager_launcher`) instead of the worker default — unless the task carries its own `launcher:`. A vault with neither falls back to its `claude_script` (better open wrong than not open). Server: `applyHumanLauncherRule` in `server/spawn-cwd.mjs`, applied by `spawn_agent` **whenever the caller passes `role: human`**, with or without a task (the server does not read `role:` from the task file). The resume path applies the same rule in `restart-worker.py` `_human_launcher`, reached through `scripts/resolve-launcher.py` — the `-claude` sibling ahead of a configured `manager_launcher`, exactly as the server does it.
- A previous run's "leave them closed" answer never carries to the next run.

## Labelling

Every value filled from a default is printed as `🏷️ defaulted <field>=<value>` in the open report, and named with its rule (`role-mode-matrix Rule 1/2`) at creation, so the operator can object while it is still cheap.
