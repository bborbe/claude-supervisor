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
| `agent`, body carries one of the five watch-need signals | `interactive` |
| `agent`, body carries none of them | **`headless` (default)** |

The five watch-need signals are listed exhaustively in [`docs/fleet-surface.md` § Spawn a worker](fleet-surface.md#spawn-a-worker) item 6 — the signal list stays there; this file owns only the mapping and its defaults.

**An `agent` row carrying none of the signals opens `headless` even when its `mode:` on disk reads `interactive`.** Operator ruling, 2026-11-07: an agent row never opens interactive. This **narrows** the both-ways carve-out at item 6, which otherwise honours a declared `mode:` in both directions; that carve-out still governs every other case — `human`, `manager`, and an `agent` row that *does* carry a signal. The normalisation prints `normalised mode interactive -> headless` and rewrites the field, so the correction is visible on disk and the next run reads the corrected value rather than re-deriving it.

## Rule 3 — `phase`

A missing or empty `phase` is not decided here: it takes the existing entry path (`vault-cli task work-on` advances it; on the `--flagged` batch the operator's flag is the approval).

## Rule 4 — how a `role: human` row opens

- Always opened, into the **Direct** window, as the operator's own session — never skipped, never as a worker.
- On the vault's **Claude launcher** (the `-claude` sibling of `claude_script` when it exists, else `manager_launcher`) instead of the worker default — unless the task carries its own `launcher:`. A vault with neither falls back to its `claude_script` (better open wrong than not open). Server: `applyHumanLauncherRule` in `server/spawn-cwd.mjs`, applied by `spawn_agent` **whenever the caller passes `role: human`**, with or without a task (the server does not read `role:` from the task file). The resume path applies the same rule in `restart-worker.py` `_human_launcher`, reached through `scripts/resolve-launcher.py` — the `-claude` sibling ahead of a configured `manager_launcher`, exactly as the server does it.
- A previous run's "leave them closed" answer never carries to the next run.

## Labelling

Every value filled from a default is printed as `🏷️ defaulted <field>=<value>` in the open report, and named with its rule (`role-mode-matrix Rule 1/2`) at creation, so the operator can object while it is still cheap.
