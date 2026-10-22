---
description: Open a name through the a2a-worker-controller — resolves it with /supervisor:open's own gates, then creates every worker on the controller, the backend chosen by the task's `target:` frontmatter. Manager-tier, like /supervisor:open. Name via $ARGUMENTS; optional --task/--goal/--topic, --vault, --first-step, --flagged.
argument-hint: "<name> [--task|--goal|--topic] [--vault <v>] [--first-step <text>] [--flagged]"
allowed-tools:
  - Bash(vault-cli:*)
  - Bash(grep:*)
  - Bash(ls:*)
  - Bash(find:*)
  - Bash(python3:*)
  - Bash(awk:*)
  - Read
  - TodoWrite
  - Agent
  - mcp__supervisor__spawn_agent
---

`/supervisor:open`'s verb, with one difference: **every worker it creates is created by the a2a-worker-controller** — never by `spawn_agent`'s local path and never by a wezterm tab. The backend is picked by the task's own `target:` frontmatter.

Manager-tier, like `/supervisor:open` (`docs/session-tiers.md` § Command → tier): it resolves a name and dispatches, and never does the work itself.

```
/supervisor:open-a2a "<name>"
  ├─ resolve + gate → /supervisor:open § Step 0 · § Step 0.5 / 0.6 · § Step 1 · § Step 1.5, run by reference
  └─ dispatch (this file's own), by the row's `target:` frontmatter:
       absent         → the controller's `local` backend, `a2a-local-control.py create`
       docker | k8s | k8s:<cluster> → `spawn_agent`, the value passed verbatim
       anything else  → `spawn_agent`, verbatim, so the server refuses it there
```

⚠️ **There is no JUMP branch and no RESUME branch here, and that is the design rather than a gap.** A controller-created worker holds no local pane to jump to, and `resume` is refused on every A2A target — so `/supervisor:open` § Step 2A's branch logic does not apply to this command and is deliberately not referenced. The dispatch below is create-only.

## Arguments

**Parse `$ARGUMENTS` yourself — never use positional argument references** — a title is multi-word, so a positional resolves to one word of it.

- **name (required):** the task / goal / topic title or identifier — everything that is not a recognised flag. Quote it when passing to `vault-cli`.
- **`--task` / `--goal` / `--topic`, `--vault <name>`, `--first-step <text>`, `--flagged`:** exactly as `/supervisor:open` defines them — read them in `${CLAUDE_PLUGIN_ROOT}/commands/open.md` § Arguments, and do not restate them here.
- ⚠️ **`--color` has no consumer on this command.** A controller worker opens no local tab, so `/supervisor:open` § 3.0's role-map colour resolution never runs; the flag is dropped rather than half-honoured.
- ⚠️ **`--first-step` rides the `prompt` on the `spawn_agent` branch** exactly as `/supervisor:open` § Step 3 defines it. On the local-backend branch it is sent as `--first-step "<text>"`; the controller tells the worker `<first_step>\n\n/vault-cli:work-on-task "<task>"`, the same prompt `/supervisor:open` builds.

If no name survives parsing **and `--flagged` was not given**, print `❌ Pass a task, goal or topic name.` and STOP.

## Step 0–1.5 — resolve and gate, by reference

**Read `${CLAUDE_PLUGIN_ROOT}/commands/open.md` and execute its § Step 0, § Step 0.5 / 0.6, § Step 1 and § Step 1.5 exactly as written.** They own, in order: the vault resolution, the `--flagged` batch with the `role` / `mode` classification, the type resolution, and the three spawn gates (approval, readiness, the fleet cap). None of them is restated here — a second copy is a second counter, and this file's whole point is that it is thin where that one is thick.

⚠️ **One substitution inside the referenced batch, and it is the only one:** § Step 0.6's per-row delegation invokes `/supervisor:open "<task>"`. Here it invokes **this** command, `/supervisor:open-a2a "<task>"` — otherwise the batch would spawn local tabs and this command would be bypassed on its own batch path.

⚠️ **Gate 3 (the fleet cap) is a `spawn_agent`-side check.** Its numbers have one home — `docs/fleet-surface.md` § Spawn a worker item 5 — and are never restated. It runs on § 2.3's branch; the local-backend create at § 2.2 never reaches that call, so nothing here re-implements it and no `operator_named` marker is passed on that branch.

## Step 2 — dispatch: every spawn goes through the controller

Everything above is `/supervisor:open`'s, run by reference. This is the half that is not.

### 2.1 Read `target:` — it picks the backend, and nothing else does

```bash
vault-cli --vault "<vault>" task get "<task>" target
```

⚠️ **The field is the task's own frontmatter, read at the call site — never from the supervisor's config file and never from the environment.** ⚠️ **One exception, and it is a per-vault value rather than a fleet-wide one:** when the row carries no `target:`, read the vault's own `a2a_default_cluster` and pass `target: k8s:<name>` — `docs/fleet-surface.md` § Spawn a worker item 7 carries that rule and the reason a per-vault value does not weaken the per-call-only one. An absent field with no vault default is not `local` by default here: it selects the controller's **local backend**, which is a different thing from `spawn_agent`'s local path.

⚠️ **Then check ELIGIBILITY — a separate question from which backend, read from the same item.** When the row carries a disqualifier, print a warning naming it (`⚠️ target "<t>" is not eligible for "<task>": needs <disqualifier>`). ⚠️ **Advisory, never a refusal** — print it and leave the decision here; never silently drop the `target`, and never rewrite the row's frontmatter. The disqualifier list has its single home in `docs/fleet-surface.md` § Spawn a worker item 7 — read it there and never restate it here.

### 2.2 Absent `target:` → the vault default, else the controller's `local` backend

**First, the vault's own default.** Read it and, when it is set, hand the row to § 2.3 as `target: k8s:<name>` — do not reach the local backend:

```bash
vault-cli config list --output json \
  | python3 -c 'import json,sys; v="<vault>"; print(next((x.get("a2a_default_cluster") or "" for x in json.load(sys.stdin) if x.get("name")==v), ""))'
```

⚠️ **A printed cluster name ends this step** — § 2.3's call is the one that runs, with the value formed here. An empty line means the vault names no default, and only then does the row fall through to the controller's local backend below. ⚠️ **Never substitute a name when the read fails** — an unreadable or unparsable `config list` is an error to report, not a vault with no default, because the two are otherwise one indistinguishable observable and the second silently moves the worker to a different cluster.

**With no vault default**, the row takes the controller's `local` backend:

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/a2a-local-control.py" create --task "<task>" --vault "<vault>" [--first-step "<text>"]
```

It prints `worker_id: <id>`, then the reply, redacted. The backend is served at `http://localhost:8092`, overridable with `--endpoint` then `$SUPERVISOR_A2A_URL_LOCAL` — ⚠️ **the local backend has its own URL variable**, and the docker controller is a different service on this Mac, so the two must never share one.

⚠️ **The create call carries `task`, `vault` and (when given) `first_step`.** `vault` is the resolved vault from Step 0–1.5: the controller reads the task's launcher, role and goals from that vault, starts the tab in its directory and writes the bind there. `cwd` is never sent. Omitting `--vault` falls back to the controller's own configured vault — never omit it.

⚠️ **The reply carries `launcher` and `launcher_source`, because this backend resolves the launcher itself.** The controller resolves the launcher in its OWN process, and ⚠️ **as of 2026-10-22 it still applies the older goal-inheriting rule** — the task's `launcher:` frontmatter, else its goal's, else the vault's `claude_script` — while `spawn_agent` has dropped the goal rung (rule home: [`docs/fleet-surface.md` § Spawn a worker](../docs/fleet-surface.md#spawn-a-worker) item 8). So a local create under a goal carrying `launcher:` can still come back on that goal's launcher; read `launcher` and `launcher_source` back and do not assume they match the `spawn_agent` arms. `launcher_source` names which of `task` / `goal` / `vault` won. So a local create is not launcher-blind: read both fields back and check them as you would on the `spawn_agent` arms.

⚠️ **A `launcher:` that is not an executable file is refused before any tab is opened**, which is stricter than the `spawn_agent` arms — those validate the value's shape only, because a bad launcher there surfaces as an unreadable `model: null`, an observable this arm does not have (`wezterm cli spawn` reports success for a tab whose `exec` then fails). ⚠️ **The refusal reason is not on the reply:** the FAILED path carries no detail, so a create that fails for this reason reports only that it failed — the named error is in the controller's own log. Reconcile against `list` (§ Step 3) as with any other FAILED outcome.

⚠️ **Exit 2 (`CONTROLLER_FAILED`) is an UNKNOWN outcome, not a failed one.** The failure can land in the controller's task store *after* the executor ran, so a create that reads FAILED may still have started a worker. Reconcile against `list` (§ Step 3) before anything else; never retry blindly.

⚠️ **The other exits are the call's own, and none of them means a worker was created:** `1` TRANSPORT (unreachable, or a non-200), `3` UNREADABLE (the reply was not a readable control response for the operation sent), `4` NO_TOKEN (no control token could be resolved). Report which one and stop.

⚠️ **Never read a token out of a reply.** The create reply carries the worker's own token beside its record; the script redacts any token-shaped key rather than trusting a later filter, and so must anything you print.

### 2.3 `docker` / `k8s` / `k8s:<cluster>` → `spawn_agent`, the value verbatim

```
mcp__supervisor__spawn_agent(prompt='/vault-cli:work-on-task "<task>"', task="<task>", vault="<vault>", label="<task>", target="<value>")
```

Pass the frontmatter value **verbatim** — never lower-cased, never mapped, never defaulted.

⚠️ **On this branch the argument contract is different, and `role` is dropped with it.** `role`, `interactive`, `resume`, `env`, `policy`, `window_id` and `shipping` are each refused, and `task` is required — the list and its reasons have their single home in `docs/fleet-surface.md` § Spawn a worker item 7; read it there and never hand-build this branch from the line above.

⚠️ **A `goal_loop: true` row is refused here, not spawned.** This path reads the task's frontmatter at Step 0.6 but never runs `scripts/goal-condition.py`, so the goal would be silently absent — a downgrade with no error. Print `⛔ goal_loop not supported on open-a2a — use /supervisor:open` and spawn nothing. Rule: `docs/fleet-surface.md` § Spawn a worker item 10.

**The `mode:` carve-out is inherited here and consumed WITHOUT an argument — do not invent one.** The row's `mode:` was already written by `/supervisor:open` § Step 0.6, and the classifier that produced it has its single home in `docs/fleet-surface.md` § Spawn a worker item 6: it runs in both directions — `interactive=false` for a row whose `mode:` reads `headless`, `interactive=true` for one that reads `interactive`, and the argument omitted only when the field is absent. ⚠️ **Neither branch of this command can carry that argument** — a `docker` / `k8s` row refuses `interactive` whenever it is present (item 7), and the local-backend create carries no mode field — so the field is read and reported, never passed. Say so in the report rather than implying it was consumed.

⚠️ **Requires `mcp__supervisor__*` in this session.** There is no wezterm fallback on this command: a tab is not a controller worker, and opening one would answer a call that asked for somewhere else.

### 2.4 An unknown `target:` → pass it verbatim, and relay the refusal

A value that is neither absent nor `docker` / `k8s` / `k8s:<cluster>` is not this command's to interpret. Make the same call as § 2.3, with the value **verbatim**, so the server refuses it there — then **relay the server's refusal text verbatim** and stop.

⚠️ **Never default it, never substitute `local`, never lower-case it.** A worker created on this Mac under a call that asked for somewhere else looks exactly like a working call, which is why the refusal is the safe direction and the design.

⚠️ **Never quote the refusal from memory or from this file.** The string belongs to the server; a copy of it here is a second text that goes stale silently, so relay what the call actually returned and nothing else.

## Step 3 — address the worker, and read its session id

A controller-created worker is addressed by its **`worker_id`** — always, on every backend. ⚠️ **A `pane_id` is not an alternative to it, and it exists on only one backend.** The controller populates `pane_id` for a worker that runs in a WezTerm tab — the `local` backend — and leaves it **empty** for `docker` / `k8s`, whose workers answer at their own `a2a_url`. So the field is the local worker's jump handle and nothing at all elsewhere; address by `worker_id` regardless, and read `pane_id` only to hand the operator a way into a local worker's tab.

**Read `bind` from the create reply** — the script prints it as one line, with the same semantics as `spawn_agent`'s `bind` in `/supervisor:open` § 4.1:

- `bind: stamped` — the task's `claude_session_id` was empty and now names this session (`session_id: <uuid>` is printed above it).
- `bind: appended` — the task was already owned; this session was appended to `metrics_sessions`, the owner left intact.
- `bind: error <msg>` — a session was found but the vault write failed. Reconcile by hand per `/supervisor:open` § 4.1 (read the owner first; prefer `append-metrics-session`).
- `bind: none` — no session registered within the controller's ~8 s bind window. Not a failure: the worker is running, and the binding may still land. Fall back to `list`:

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/a2a-local-control.py" list
```

One worker per line — `<worker_id>  session_id=<id>` — and a worker the controller has not stamped yet prints its id alone.

## Step 4 — report

State the branch taken in one line, plus the resolved type and the resolved vault, so a mis-resolution is visible immediately:

- local backend → `🆕 created (local) · worker <worker_id> · pane <pane_id> · session <sid-prefix> · bind <stamped|appended|error|none>`
- `docker` / `k8s` → `🆕 created (<target>) · worker <agent_id>` — read the `target` back from the response rather than assuming it.
- unknown `target:` → the server's refusal, verbatim.

⚠️ **Report a `pane_id` for a `local` worker, and never in place of the `worker_id`.** Local is the one backend that populates it — the worker runs in a WezTerm tab, so the pane is the operator's way in. For `docker` / `k8s` the field is empty, and a pane id printed there reads as a handle to a session that cannot be jumped to.

## Notes

- Manager-tier (`docs/session-tiers.md` § Command → tier), like `/supervisor:open` — it resolves a name and dispatches; the work happens in the worker.
- The resolve-and-gate half is `/supervisor:open`'s, referenced rather than copied; this file owns only the dispatch, and stays thin on purpose.
- Invoked as `/supervisor:open-a2a`.
