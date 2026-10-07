# Fleet surface — spawn shape and table render spec

The canonical, self-contained home for the two operational specs the fleet commands
(`/supervisor:manager-loop`, `/supervisor:fleet-loop`, `/supervisor:fleet-status`,
`/supervisor:manager-status`) depend on.

These specs previously lived in an Obsidian vault runbook and were referenced by absolute
path, because Obsidian wikilinks do not resolve across vaults. A plugin cannot depend on a
particular person's vault to describe its own mechanics, so the operational content lives
here and the vault runbooks keep only the per-vault operating procedure.

Change the shape here, then update every call site, then re-run the grep that proves no
copy diverged.

## How commands are addressed

A plugin's commands are namespaced by its install id. This one installs as
`supervisor@claude-supervisor` — `.claude-plugin/plugin.json` names the plugin `supervisor`,
`.claude-plugin/marketplace.json` names the marketplace `claude-supervisor` — so every command
it ships is addressed `/supervisor:<name>`: `/supervisor:jump`, never `/jump`.

**This section is the single statement of the rule.** The bare form resolves to nothing —
Claude Code answers `Unknown command` — and the managers hand `/supervisor:jump` to a blocked
worker as their last resort, so a bare form fails exactly when it is needed (measured
2026-09-19: the operator followed a printed handover, typed the bare form with a pane id, and
got `Unknown command`). The namespace follows the install id, so renaming the plugin or the
marketplace changes the prefix with it — a one-place edit here, not a sweep of the commands.

## Session roles — who may start what

**A manager is never started in a worker session.** A worker session carries a *task*; a manager session carries a *topic or goal* and a loop. Arming one inside the other collapses the two roles, and the collapse is silent: the session keeps its task name and its task anchor while its turns now sweep a topic's whole tracked set, and the operator sees a worker whose tab is quietly doing someone else's job. (Operator rule, 2026-09-21 — stated when a worker session proposed to exercise a one-sweep `/supervisor:manager-loop` run as its own end-to-end check.)

The direction that **is** allowed runs the other way: a manager starts workers (§ Spawn a worker), and a worker reaches its manager over `SendMessage`. **Starting a manager is a human act** — the operator invokes `/supervisor:manager-loop <subject>` in a session created for that purpose.

**Dispatch authority is the manager's.** Only a manager opens sessions — the manager-loop for its topic or goal, the fleet-loop fleet-wide — under **one fleet-wide concurrent limit** (§ Spawn a worker item 5). The worker-side rule lives once, in the global rule `worker-does-not-open-sessions` at `~/.claude/claude-md-rules/worker-does-not-open-sessions.md`; that file carries the prohibition, and it is **not restated here**. A worker that finds work outside its anchored task routes it to its manager, per the rule above.

⚠️ **There is no one-sweep mode — use `/manager-status` to look and `/manager-drive` to act once.** `manager-loop` once carried a flag that suppressed the arming and nothing else: the Guardrails still ran, so a single sweep could spawn up to 2 sessions, auto-resume a dead worker, auto-compact one over 70% and reconcile the topic page. **"One sweep" is a cadence limit, not a blast-radius limit.** The flag was removed because its name promised a read-only look it never gave (read that way, and caught, on 2026-09-21); `manager-status` + `manager-drive` split the look from the act honestly.

⚠️ **The fleet side now has the same split — use `/fleet-status` to look and `/fleet-drive` to act once.** `fleet-loop` once required a `loop` argument to repeat; a bare invocation ran one round and stopped, and a round dispatched nothing at all — no drive leg, so parked-but-unblocked sessions sat until someone ran `/fleet-drive` by hand. That is the manager-layer shape missing one leg, not a smaller blast radius: the round still swept, classified and wrote the snapshot, so "one round" was a cadence limit there too. The argument was removed on the same reading as the manager flag — a bare `/supervisor:fleet-loop` repeats by default and dispatches the drive leg every round, and `/fleet-drive` is the by-hand single pass. **The two layers are mirrors: `manager-loop` : `manager-drive` :: `fleet-loop` : `fleet-drive`.**

⚠️ **A worker session therefore has no end-to-end check of a manager command.** Exercising one belongs to a manager session's own runtime. A change whose verification is "run the manager and watch it behave" is verified by deployment (the installed copy carries the change) plus a lockstep grep across the copies — never by arming a loop from wherever the change was authored. **Handing that run to the manager is not a wait:** an idle manager drains `SendMessage` only on its next turn, and nothing gives it one. Send with `notify_when_idle: true` and close the worker's turn 🔵 READY with `you run: <command>` for the operator to type in the manager's tab — never 🟡 WAITING on the reply. (Measured 2026-09-23: a `/supervisor:reset` handoff sat unread ~4.5 h.)

⚠️ **And some acceptance checks no role may run — the three-way exclusion.** The paragraph above hands the check to the manager. Some checks admit no such hand-off: they need one session that **both** spawns a tab worker **and** then reads it back, because `agent_status` resolves only the workers its own server spawned (`agents.set()` fires inside `spawn_agent`, `server/supervisor.mjs`). No role holds both halves.

- **A worker may not spawn** — `worker-does-not-open-sessions` (§ Dispatch authority above).
- **A manager may not verify** — the manager delegates; a verification is work whatever its size (`65 Runbooks/Manager Session.md` § Guardrails item 2, and the line there is work vs management, not read vs write).
- **Only the operator's own session remains** — and only once they lift the first prohibition.

The peer's framing, verbatim: *"a worker may not spawn; a manager may not verify; so only a session the operator starts can close it."* **Each leg is a deliberate contract and none is individually wrong; their intersection is empty**, which is why no source named it until 2026-09-27. ⚠️ **State it as policy, not mechanism:** nothing in the harness blocks `spawn_agent` from a worker, so a reader told a worker *cannot* spawn goes hunting for an enforcement that does not exist. The operator's authorization is the bridge, and the check becomes takeable the moment they give it. A session that meets this shape must **stop and say so**, rather than attempting the check and discovering the prohibition afterwards. (2026-09-27, session `cd816ba7`.)

## Session end — the disarm contract

`/supervisor:manager-loop` arms a loop. **`/supervisor:stop` stands it down**, and this section is the contract that command points at: what it must disarm, which harness surface reaches each driver and which it cannot, and what must survive.

**The three drivers, and what reaches each.** Two of them can be read and one cannot, and the reachable set is smaller than the list.

| Driver | Wakes the model? | Reached by | Reachable from inside the session? |
|---|---|---|---|
| a `CronCreate` job — **which is what every `ScheduleWakeup` loop is** | yes | `CronDelete <id>` | **yes** — `CronList` enumerates this session's own jobs |
| a `Monitor` / background task | yes — each emitted line is a turn | `TaskStop <task_id>` | **only while the id is still in the conversation.** The harness exposes no enumeration of in-session background tasks, so the command cannot discover one it cannot name. A `Monitor` is also **bounded** (≤30 min), so it is not a durable cadence driver whatever the tick uses it for |
| the detached gate loop | **no** — 0 model tokens, ~330 ms a tick | `kill <pid>` | reachable, and **deliberately not touched** |

**`ScheduleWakeup` is not a second driver — it is a `CronCreate` job, so it shares that row's read surface and its kill path, and there is no other one.** Measured 2026-10-03: a `ScheduleWakeup` call produced a `CronList` entry carrying the probe's own prompt (`c0b46632 — Every day at 9:59 PM (one-shot) [session-only]: GAP3-DRIVER-PROBE …`), and `CronDelete c0b46632` removed it — `CronList` then read `No scheduled jobs.` ⚠️ **`ScheduleWakeup {stop:true}` is NOT that kill path, and an earlier version of this section said it was.** Measured 2026-09-30 on a live loop: `CronList` showed the armed job twice (`1778c367`, `9e4cc484`), each carrying the loop's own prompt, while `ScheduleWakeup {stop:true}` returned *"there was no pending wakeup to cancel."* A `stop` that followed only the `ScheduleWakeup` row therefore left the loop armed and re-firing, and that stand-down completed only because `CronList` was read first. The kill path is `CronList` → `CronDelete`, the same one the row above gives.

**That leaves exactly one driver without a read surface, and it is what decides what `stop` may claim.** A `CronCreate` job — including every `ScheduleWakeup` loop — can be **read**: `CronList` enumerates this session's own jobs, so the command knows both before and after whether one existed, and may report `disarmed` or `not armed`. A `Monitor` can be **stopped but not enumerated**, so it can never be reported as disarmed, and the command's report carries its third form for that one driver alone. ⚠️ **And it may be neither stopped nor read:** with no task id left in the conversation there is nothing to name, so no stop is sent at all and the honest line is *no id to stop* — reporting that as *stop sent* claims an act that never happened, the same over-claim as a `✓` the harness could not support, one form down. Measured 2026-09-22, on the first live run of the command: the model reached for `~` for the `Monitor` line, found "stop sent" false, and printed a form the template did not yet carry rather than assert it.

**Interrupting a turn disarms nothing.** Every driver above survives it, so the next firing arrives on schedule. That is why the verb exists rather than an interrupt.

**What must survive `stop`:**

- **The model-free gate loop.** Hosted by launchd (`com.bborbe.sweep-gate-notify`, a 900s `StartInterval` job) since 2026-09-22, when the detached `sweep-gate-ledger-loop.sh` host was retired. It writes **per-vault** state under `~/.claude/state/sweep-gate-loop/<vault>/` — the launchd host exports `SWEEP_GATE_STATE_DIR` to place it there — and it is what keeps `<vault>/<subject>.tick.txt` truthful. ⚠️ **It is not the store `/manager-status` replays from.** That command's no-change path reads its own plugin-owned snapshot at `~/.claude/state/manager-predispatch/<subject>.json`, written by `scripts/manager-predispatch.py` when the command itself runs. The two carry the **same digest inputs** — the loop's per-subject snapshot was compared field-for-field against the gate's on 2026-09-26 and they agree — ⚠️ **with one deliberate divergence since 2026-10-05:** the gate's digest now hashes a **death-only** liveness term (`liveness_change_term` in `scripts/manager-predispatch.py`) so session churn cannot authorise a dispatch, while this snapshot keeps the raw `liveness` word for the render and its writer keeps its own deliberate `liveness -> parked` trigger (a different gate with a different job). The field SET is unchanged; the divergence is by design, not drift — but they are different files serving different surfaces, and the loop's is loop-only: it exists for subjects with an armed loop, so it cannot back a command that runs against arbitrary subjects. Killing this job freezes the tick file; it does not touch the gate's snapshot. **The per-subject snapshot is `<vault>/<subject>.snapshot.json`, its writer is `~/.claude/scripts/sweep-gate-classify.py` (`save_snapshot()`, which writes the snapshot and appends the history record), and its schema has its single home here:** `recorded_at` (the sweep's timestamp — the value that identifies *which* sweep a row set came from; ⚠️ **it advances only when the gate's digest CHANGES, never on every tick** — `sweep-gate-notify-tick.sh:137–141` invokes the writer only on the CHANGE-WAKE branch (gate exit 10), and the EMPTY branch writes *"heartbeat + ledger only"*, so a quiet subject's `recorded_at` freezes for hours while its loop is perfectly healthy. **It is an identity, never a liveness signal** — read as one it reports a stopped loop on a healthy subject, which is how the row [[The Sweep-Gate Snapshot's recorded_at Reads as Freshness but Records the Last Digest Change]] was filed), `tasks` keyed by task name with `{status, phase, met, stuck, liveness, progress_hash, session}`, plus `events` and `fail_open`. Beside it sits an append-only `<subject>.snapshot.history.jsonl` — one record per sweep, never overwritten, which is what makes the prior sweep's state readable at all. ⚠️ **The schema carries no bucket concept** — buckets are the *caller's* classification (`agents/manager-sweep-reader.md`), computed from the vault runbook, so a per-bucket name set is never obtainable from this file. ⚠️ **This block is the schema's one home; cite it, never restate it** — the same rule § Spawn a worker item 5 carries for the cap. Note what the schema does **not** carry: there is no task-file mtime, so a freshness reading cannot be taken from it (`agents/manager-drive.md` step 2 Check 1 takes it from the caller's report instead) — **and `recorded_at` is not a substitute for one**, per the parenthetical above. **The liveness signal is the heartbeat**, `<vault>/<subject>.heartbeat`, written on **every** tick whatever the digest does (`sweep-gate-notify-tick.sh:161`); read *that*, never `recorded_at`, to answer *"is this subject's loop alive?"* `stop` prints the heartbeat's age and the tick file's mtime as evidence it survived, and never signals it. Killing it leaves the operator a frozen table and a `/manager-status` reporting a tree nobody is watching. ⚠️ **The flat `~/.claude/state/sweep-gate/<topic>.*` tree is NOT retired — the earlier claim here was wrong, and the error mattered.** This line previously read *"nothing writes it any more … (verified 2026-09-23: no detached loop running, every topic's state there stale)"*. Re-measured **2026-09-26**: the tree is **live** — `attention-routing.tick.txt` written 17:43, `attention-routing.{arms,cadence,json}` at 17:43–17:44. What the original line got right is the *kind* of state: the flat tree carries **loop-cadence records** (`.cadence`, `.arms`, `.stopped` — see `docs/restart-worker.md:83`), not gate state, and the gate loop does not write it. All three gate scripts resolve the per-vault tree instead (`sweep-gate-notify-tick.sh:52` and `sweep-gate-notify-stale.sh:13` both `BASE="$HOME/.claude/state/sweep-gate-loop"`; `sweep-gate-adhoc.py:49` likewise), which is why a reader resolving **gate** state in the flat tree finds nothing. ⚠️ **The writer of the flat tree's cadence records is not yet identified in this repo** — treat "who writes `~/.claude/state/sweep-gate/<topic>.cadence`" as open rather than assuming the 900s job, and do not let a tick-file *absence* test in the flat tree stand in for a gate-state check. A `*.tick.txt` **does** exist there for live topics, so the file is real, not a fossil.
- **The session.** The cadence is session-scoped, but the asks ledger is keyed by session id — `~/.claude/state/open-items/<session-id>.json` — so closing the session and re-opening the topic mints a new id and an empty ledger, and every open entry vanishes silently, including the `asked-of-you` entries only the operator can resolve. Measured 2026-09-22: session `433c856d` held **10 open entries, 2 of them `asked-of-you`**, and the session that replaced it carries the same 10 re-added **by hand** — the workaround, not a mechanism.

**`stop` is not a close.** It writes no page (its one state write is the `<slug>.stopped` marker, so the liveness watcher reads the silence as a stop rather than a lapse), the topic's `status` is byte-identical before and after, and it never offers `/vault-cli:session-close`. **Restart is the same command that started the loop** — `/supervisor:manager-loop "<subject>"` — so there is no `start` verb and none is needed.

⚠️ **Probe the gate loop by the script argument's basename, never by a substring of the command line.** `pgrep -f sweep-gate` matches any process whose argv merely *mentions* the path, and a manager's spawn prompt quotes it — measured 2026-09-22, the substring probe returned **three** pids for one loop, two of them the worker sessions spawned from a prompt naming the script. **The count is transient; the mechanism is not** — re-run hours later those two workers had exited and it returned one, while a bystander process whose argv merely carried the string reproduced the spurious pid on demand. `scripts/stop-probe.py` matches the second argv token's basename against the two known script names, which also fails in the safe direction: a path containing a space mis-splits and the probe under-reports rather than inventing a loop.

## A plugin install does not reach a running session — the probe and the lever

**A plugin command body is read from disk when the session starts, not at each invocation**, so
installing an updated plugin does not change what a running session executes. It keeps serving
the body it loaded, indefinitely, with nothing announcing the change. The failure is silent in
both directions: a session can hand the operator a stale procedure for hours while reporting
success, and the lever everyone reaches for by default — a full restart — is heavier than the
job needs.

**The probe: a running session can read which version it loaded.** The harness writes
`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/.in_use/<pid>` at session start — a
JSON file `{"pid":<n>,"procStart":"<date>"}` naming the version directory that session holds.
`CLAUDE_PID` is exported into the session's own Bash, so the version you are serving is the
**newest** entry carrying your own pid:

```bash
ls -dt ~/.claude/plugins/cache/claude-supervisor/supervisor/*/.in_use/"$CLAUDE_PID" | head -1
```

⚠️ **Newest, not "whichever matches" — a reload writes a new entry and never removes the old
one, so a reloaded session's pid sits in several version directories at once.** Measured
2026-09-27 on one session: before `/reload-plugins` its pid `83726` was in `0.61.1/` alone
(mtime 18:13); after, it was in **both** `0.61.1/` (18:13) and `0.62.4/` (20:15), with `0.62.4`
installed. Multi-membership is the normal case, not an edge — the same inventory shows pid
`56704` in nine version directories. A bare `ls -d …/.in_use/"$CLAUDE_PID"` therefore prints
two or more paths after any reload and cannot be read unambiguously; `-t` (newest first) is
what makes it a probe rather than a list.

Compare the result against `installPath` in `~/.claude/plugins/installed_plugins.json`. A
version directory that is not the installed one means this session predates the install.
Measured 2026-09-27: session pid `83726`, `procStart` `16:13:06Z`, was recorded in
`0.61.1/.in_use/83726` while the install read `0.62.1` — the plugin moved four versions inside
that one session, and nothing in the session said so. **The same session's `/reload-plugins`
then moved that entry to `0.62.4`, so the probe reports recovery as well as staleness** — which
is the property that lets this section's clause promise a lever at all.

**The lever is `/reload-plugins`; a restart is not required.** It reloads plugins, skills
(including every `commands/` entry), agents, hooks, plugin MCP servers and plugin LSP servers,
and it re-reads plugins from disk, so it also switches to a new version's cache path. Restart
only if, after `/reload-plugins`, the served body still cites the old version path. The vault's
*a Claude Code guide* owns the per-artifact-class lever table; this section
names the lever and points at that table rather than copying it.

**The fleet reading is `scripts/plugin-version-census.py` — the probe above answers "am I
stale", this answers "who is".** It takes the live sessions from `session-liveness.py --list
--json` (never from the cache — see the trap below), resolves each pid's **newest** `.in_use`
marker, and prints one header plus one line per stale session, each carrying the jump target
resolved through the tty join:

```
installed: 0.114.1 · live: 44 · stale: 21 · unknown: 0
  65d26b6c  loaded 0.114.0  Attention Manager  /supervisor:jump 3
```

⚠️ **The header carries a fourth segment, `· unknown: N`, whenever any live session has no
marker** — the normal headless-worker / cluster-worker case, and the bucket a caller must not
fold into `stale`. It is omitted only when it is zero. `installed: unknown` is a second kind of
`unknown`: with no baseline to compare against, every row reads `unknown` rather than `current`,
so an unreadable `installed_plugins.json` never prints as a clean `stale: 0` fleet.

`--json` emits the same rows for a caller that renders its own table. **`--reload` is the
lever**: it activates each stale session's pane, types `/reload-plugins` into a pane whose
composer is **empty**, and confirms the reload by **the newest marker for that pid moving**.
⚠️ Never by the pre-reload version directory's mtime: a reload writes the new entry into the
*installed* version's directory and leaves the old one alone, so watching the old path can
never fire and would report every successful reload as unconfirmed. Never by the pane's echo
either, which is identical whether the session was idle at its prompt or mid-turn.

⚠️ **"Empty" includes the TUI's placeholder hint, and that is not the same test
`server/tab.mjs:isReady()` makes.** An idle Claude Code composer reads
`❯\xa0\x1b[0;2mTry "…"` — the hint is drawn **dim** (SGR attribute 2) and nobody typed it.
`isReady()` refuses it, correctly for its own job: it gates a **spawn**, where the TUI may
still be painting and a submit sent into that phase is swallowed (measured 2026-09-20).
Applied to this lever, whose whole purpose is a **long-idle** session, that rule refuses
every session it exists to reach — measured 2026-10-07, all 38 stale sessions returned
`composer not empty`. The census accepts a composer whose remainder is empty **or** dim, so
text somebody typed is still refused (2026-10-05: `❯ draft ok` parked 3h22m) while the
placeholder is not. The readiness read is `get-text --escapes` for exactly this reason.

⚠️ **The marker count is not the session count.** A reload writes a new `.in_use/<pid>` entry
and never removes the old one, so counting markers over the cache reads **17 versions / 81
markers** against a fleet of **44 sessions / 8 versions** (measured 2026-10-07) — a five-fold
overstatement, in the direction that looks like more evidence. The reading is the newest
marker per live pid, which is the same `-t` rule the per-session probe uses.

⚠️ **The lever reaches TAB sessions only.** A headless worker is an in-process `query()` and a
cluster worker runs in a pod, so neither necessarily holds a local marker or a pane; a session
with no resolvable pane is reported as such per row rather than skipped. Measured 2026-10-07:
`boss` runs on a tty no wezterm pane owns and is the standing example.

⚠️ **A peer message is not a lever — measured 2026-10-07.** A cross-session `SendMessage`
asking a session to reload is delivered and **cannot be acted on**: `/reload-plugins` is a
built-in CLI command, not a Skill, so the recipient has no tool for it and says so; the marker
did not move. `wezterm cli send-text` into the pane is the mechanism that works, and it is what
`--reload` uses.

⚠️ **Two probes that do not discriminate — both measured 2026-09-27.** The **skill listing**
cannot tell a stale session from a current one: command frontmatter descriptions are routinely
unchanged between releases, and all 21 of this plugin's commands were byte-identical across
three consecutive versions (`diff -rq` over `commands/` and `docs/` → no differences). And the
**MCP server** is a different artifact class from `commands/*.md` — this plugin's runs from a
workspace checkout (`bun run --cwd …/claude-supervisor/server`), never from the version-pinned
cache, so probing it says nothing about the command body. A check that is green on both copies
is not a check.

**This section is the single statement of the rule.** `docs/session-tiers.md` and the vault's
*a Claude Code plugin guide* point here; neither restates it.

## Spawn a worker

**This section documents `/supervisor:open`'s internals — the spawn shape it produces, and the mechanics every spawn site shares — not a parallel path to use instead of it.** The rule for when a manager dispatches through `/supervisor:open` rather than a bare spawn is stated once, in `docs/session-tiers.md` § The manager boundary; what follows is what that command does under the hood, so read it to understand or debug a spawn.

**Readiness precondition — author and score the task before any spawn, or the worker's own gate parks.** A task the manager hand-writes usually ships without `# Tasks` and `# Definition of Done`, so the worker's own `plan-task` gate stops and asks the operator to supply the decomposition — inside the worker's pane, as a multi-question wizard that cannot safely be relayed. Measured 2026-09-19: three hand-written task files produced **three 3-question wizards**, nine operator decisions, none of which needed the repo open. So, before spawning:

1. **Author the task through `/vault-cli:create-task`** — the wrapper that dispatches the `task-creator` agent, which emits exactly the sections `plan-task` needs: `# Success Criteria`, `# Definition of Done`, `# Tasks`.
2. **Score it with the `task-auditor` agent** (`vault-cli:task-auditor`) — **and let the readiness ladder decide the row.** A manager gate looser than the worker's gate is decorative: an 8/10 task clears the manager and still parks the worker's `plan-task`.

   **The readiness ladder — these four branches are the rule, and this block is their single home.** Operator ruling 2026-10-01, verbatim: *"is not about 8/10 9/10 or 10/10 ... if approve is not perfect we should try to improve it ... and after we improved it we should start it even at 7/10. >= 9/10 => approve and open directly; < 9/10 => try to improve; >= 7/10 => allow to approve and open after improvement"*

   ⚠️ **Every branch below is conditional on ZERO hard-gate failures — the score decides *which* branch, the hard gates decide *whether the ladder runs at all*.** An audit returning any hard-gate failure is **carded and held regardless of score**: a 9/10 carrying a failing hard gate does **not** open. ⚠️ **The two conditions are joint, never alternatives** — a row is measured on both, and satisfying one does not excuse the other. Zero hard-gate failures is also the repair loop's own acceptance (`agents/manager-drive.md` clause (1) step 2), so a row that keeps failing one takes the card branch however high its score climbs.

   | Post-audit score | Act |
   |---|---|
   | **any score, with ≥ 1 hard-gate failure** | post one card and hold — the ladder does not run |
   | **≥ 9**, zero hard-gate failures | open directly — no improvement pass |
   | **< 9**, zero hard-gate failures | run **one improvement pass** — the audit's named gaps fixed in the task file — then re-audit |
   | **≥ 7 after** the improvement pass | open |
   | **< 7 after** the improvement pass | post **one** board card naming the gaps, then hold |

   ⚠️ **"One improvement pass" IS the existing bounded repair loop — not a second loop, and not a new round cap.** Its rounds, its stop condition (zero hard-gate failures) and its early-stop-on-repetition rule are `agents/manager-drive.md` clause (1)'s, and the ladder changes **none** of them. What the ladder changes is only *which score opens a row*. ⚠️ **The two questions are different and must not be merged:** the loop asks *"is there more structural repair to do?"* — no, once the hard gates are clean — while the ladder asks *"may this row open?"* — not below 7 after the pass. So a row that reaches zero hard-gate failures and still scores below 7 takes the card branch, and the loop does **not** spend a round chasing the score: the measured evidence is that those rounds patch prose and converge on nothing (four audits of one unchanged row returned 6 → 7 → 8 → 8, with zero hard-gate failures at every round).

   ⚠️ **`UNFIXABLE:` is handled BY the improvement pass, not handed straight to the operator.** Operator decision 2026-10-01: when an audit reports a Success Criterion as unmeetable — it names an artifact nobody built, or a verdict no probe can evidence — the pass **rewrites that criterion into a falsifiable form, or splits it into its own follow-up task**, then re-audits; the row then opens at 7 or better like any other. The card is the **fallback**, and fires only when neither rewriting nor splitting works, or when the criterion is an operator-only decision. ⚠️ **This narrows the token's old meaning deliberately.** Before this ruling an `UNFIXABLE:` row spent no round and went to the operator verbatim; that is now the last branch rather than the first, and `agents/manager-drive.md` clause (1) step 0 is where the branch is read.

   ⚠️ **A `phase: todo` row is untouched by the ladder.** It is refused at Gate 1 before any audit runs; the ladder never opens one and never flips one `todo → planning`. That refusal's single home is `commands/open.md` § Step 1.5 Gate 1.

   **A row still below 7 after its improvement pass is not held in silence — it asks the operator.** This is an **application of a ruling already in force**, not a new exception: a decision the operator must make goes on the attention board **as a card** carrying the question, why it matters, the options with a recommendation, and the jump link — the manager page's § `## What a manager does` (primary vault, `50 Knowledge Base/a knowledge-base page.md:30`, verified on disk 2026-09-27). A below-7 row is exactly such a decision: the operator can raise the task, the manager cannot. Operator ruling 2026-09-27: *"If the quality of the task is too low, it should ask the user, human, me, to help to raise this."*

   Four points, so the interaction with the ladder is stated rather than implied:

   - **What the card says when the audit returns a score** — the score *against the threshold*, in the payload: `Raise "<task>"? It scores <score>/10 after its improvement pass, against a 7/10 open threshold.` The score is always named; a card that asks for help without saying how far short the row falls makes the operator re-run the audit to find out.
   - **Which gaps it carries** — the audit's **named gaps, verbatim**, in the context field — never a paraphrase, and never a bare count. The manager does not re-summarise them: the auditor named them, and a manager's summary is a second reading of a document the operator is about to read anyway.
   - **Who re-runs the audit** — **the manager**, on a later sweep, after the operator's edit lands. Never the operator (they were asked to raise the task, not to score it) and never the worker (its `plan-task` would park on a task the manager can still repair).
   - **What the manager does if it passes only after the operator edits it** — it opens the row on the next sweep through the normal path, and closes the card as answered; the row then proceeds through steps (3)–(6) like any other. ⚠️ **The pass is re-scored, never assumed.** An operator edit is not itself a passing score: a manager that opens on the edit alone has quietly replaced the ladder with its own judgement of the edit.

   ⚠️ **One scope exception, and its home is `commands/open.md` § Step 1.5 Gate 2 — reference it, do not restate it here.** In `/supervisor:open --flagged`, an operator-flagged row the ladder would otherwise card is **opened** instead, with its gap bullets carried into the worker's prompt as its first step. The flag is the operator's own answer to the question the card would ask, so carding a flagged row would ask it twice — and the improvement pass still runs first, so the flagged row opens *improved* rather than raw. The ladder itself is untouched; only who it holds back in the batch changes.

   ⚠️ **The one-verb path is `/supervisor:ready <task>`.** It runs this item's audit → repair → re-audit loop against **one named task**, then acts on the verdict: it opens via `/supervisor:open`, or escalates naming the gap. It **re-surfaces** the loop in `agents/manager-drive.md` clause (1) rather than adding a second one, and it **references** this section for the bar rather than restating the number — the same single-home rule the authoritative-home block below states for all four constants. ⚠️ **It readies a task; it never approves one.** A `phase: todo` row is refused with `commands/open.md` § Step 1.5 Gate 1's block *before* any audit runs, so a readying verb cannot quietly become an approval path. Cross-tier — runnable by the fleet manager and by a goal/topic manager. Its whole output is one line, and nothing else reaches the caller's context:
   ```
   READY: <task> — <before>/10 → <after>/10 — opened | escalated: <gap> | refused: not approved
   ```
3. **Check the three sections exist** before the spawn — `grep -cE '^# (Success Criteria|Definition of Done|Tasks)' <task-file>` returns **3**. A task authored through this path does, by construction; a hand-written one usually does not.
4. **Keep the split — the gate is readiness, never planning.** *Authoring* — sections, subtask decomposition, DoD, naming and SC evidence shapes — needs no repo access and belongs to the manager. *Execution planning* — which file, which mechanism, what the system actually permits — needs ground truth a manager does not have and stays with the worker. Measured counter-example 2026-09-19: a manager told a worker to "narrow the rule" on *a separate task*, and the worker found the ask-list does literal-prefix matching only and **cannot express that distinction at all**. A manager-side planning pass would have produced the same wrong plan with no wizard left to catch it.
5. **Respect the fleet-wide worker target.** New-session spawns answer to **two fleet-wide thresholds**, read from **`spawn.maxConcurrent`** (the SOFT cap) and **`spawn.maxConcurrentHard`** (the HARD cap) in `~/.config/claude-supervisor/config.json` — **not** a per-manager budget, and **not** a per-sweep or rolling-window count. **The default is 20.** An absent soft key resolves to `DEFAULT_MAX_CONCURRENT` (20). **The hard default is 50** — an absent hard key resolves to `DEFAULT_MAX_CONCURRENT_HARD` (50). `0` on the soft key means **unlimited** and is the off switch, and it switches **both** thresholds off; `0` on the **hard** key is **refused** rather than read as unlimited, because it would remove the ceiling while the soft cap stayed in force; an explicit integer is that integer. ⚠️ **This reverses the 2026-09-27 ruling** (*"These limits are artificial and should be removed … For now there is no global limit"*), under which the key shipped unset and unset meant unlimited. The operator's design of 2026-10-01 reinstated one — *"keep a total worker limit so the laptop isn't overwhelmed. Target e.g. 20"* — so the reversal is recorded here rather than left with both statements standing: a reader who finds only the 2026-09-27 note concludes the fleet is unbounded, which is no longer true.
   ⚠️ **TWO THRESHOLDS SINCE 2026-10-04, ON THE OPERATOR'S RULING** — *"lets start with 30 = soft cap and 50 = hard cap"*. The **soft** cap is where the manager loops stop proposing and an **ordinary** spawn is refused; the **hard** cap is where **every** spawn is refused, including one the operator named by hand. **The band between them — `soft <= live <= hard - 1` — is where an operator-named priority task may still open**, so the fleet never refuses the operator's own named work for a reason the operator did not choose. This **reinstates** the fleet-wide limit the 2026-09-27 ruling removed — as a pair of thresholds rather than unbounded. ⚠️ **30/50 is the operator's CONFIGURED pair, not the shipped default** — the defaults are 20/50 above, and the two differ. This sentence deliberately names no number for that reason: the operator's ruling quotes his config, and a reader who takes it for the default has read one for the other.
   ⚠️ **THE OPERATOR-NAMED MARKER has one home — `commands/open.md` § Step 1.5.** For orientation only: a task carrying the operator-set `flag: true` (`flag_set_by: operator`) is operator-named, and the spawner passes `operator_named: true` for it. Never infer it — an agent-set flag is already refused at the selector, and a default of true would make the soft cap unenforceable.
   ⚠️ **AT THE HARD CAP THE REFUSAL IS CARDED, NEVER SILENT** — the open path posts an attention-board card naming the fleet-full condition, because a refusal the operator cannot see is indistinguishable from the fleet ignoring them.
   ⚠️ **ONE PAIR, ONE RESOLVER.** Both numbers come from a single `resolveMaxConcurrent` call, so the pair cannot be held apart and a soft cap above its own ceiling **refuses** rather than running. Two keys that can disagree is the defect this repo already refused once, in the paragraph below.
   ⚠️ **THE SOFT CAP IS ALSO THE TARGET the manager loops read, and that is deliberate.** The manager loops read it to decide whether to propose work (`commands/manager-loop.md` step 4): **below** it, a tick with no ready row posts ONE card naming the next most important rows to approve; **at or above** it, no manager posts a card and no manager opens a task. Two keys — a cap and a target — were considered and rejected, because they can disagree, and a fleet capped at 20 while its managers propose against 30 is a defect with no error on it. ⚠️ **That rejected pair is `cap` + `target` — two ROLES over one population — and it is NOT the soft + hard pair above, which is two THRESHOLDS.** The distinction is the point: the soft cap **is** the target, so those two roles still ride one number and cannot disagree; the hard cap adds a second number in a role nothing proposes against, so there is no second counter to drift.
   ⚠️ **The count is live WORKER SESSIONS — the spawn ledger joined to the liveness channels — and the unit is the thing to get right.** `scripts/worker-sessions.py --count` (shell) and `server/worker-sessions.mjs` (server) count the sessions **recorded in the spawn ledger** — which is what makes a session one the supervisor opened rather than a manager or one of the operator's own — **and live**, which the session registry OR the heartbeat store answers. ⚠️ **Both channels are needed, and the union is a correction made 2026-10-05.** The registry (`~/.claude/sessions/*.json`) is pid-keyed and local-only: it sees every session holding a socket — every interactive tab — and it structurally cannot see a worker with no pid of its own, which is a headless worker (an in-process SDK `query()`) and a cluster worker (a process on another machine). The heartbeat store covers exactly those: `server/supervisor.mjs` stamps a headless worker from its agent loop, and `server/cluster-heartbeat.mjs`'s `pollCluster()` mirrors cluster sessions into the same store. ⚠️ **The count read the registry alone from 2026-10-01, so it counted the tabs and rendered both headless and cluster workers as dead** — and the fix before that read the heartbeat alone and rendered the tabs as **0**. Each was the other's mirror defect; two populations cannot be recovered by fixing one channel. Measured 2026-10-01: 26 live registry sessions, **18 worker sessions**, all four managers absent from the ledger. ⚠️ **THE COUNT CAN ONLY GO UP, SO RE-READ `spawn.maxConcurrent` BEFORE THE FIRST SWEEP AFTER UPGRADING.** A fleet that was silently under-counting now counts the headless and cluster workers it was rendering dead, so the first tick can read **at or over** the target and print `⏸️ CONCURRENCY LIMIT` — with no error on either side, which is the same silence the under-count produced in the other direction. The number did not change; what changed is that it is now true.
   ⚠️ **`live-workers.py` is NOT this instrument, and reading it here was the defect.** It reads the headless heartbeat store, which is stamped only for in-process workers (`mode: 'headless'`, written from the agent loop) — so it answered **0 while 11 interactive workers were live**. A target that reads 0 on a busy fleet is inert: managers always see "below target" and always propose, and the cap never binds. ⚠️ **`--count`, never `--list | wc -l`** on either script — the empty-store message goes to stdout, so line-counting answers **1** for an idle fleet, which reads as "one worker live" on a machine with none. A non-zero exit means a store could not be read: `UNKNOWN`, never `0`.
   **The TARGET has its own instrument, and reading it is a separate act from counting.** `python3 <plugin>/scripts/worker-target.py` prints the resolved **soft** target and nothing else; `--hard` prints the **hard** cap instead; `--source` adds `default|config|env`, which is how a bare number is told from a configured one. It calls `resolveMaxConcurrent` in `server/spawn-mode.mjs` rather than restating the rule, so the numbers a manager reads and the caps the server enforces cannot drift. ⚠️ **The count has had a read command since it shipped and the target had only this section as a pointer, and that asymmetry was measured as a live failure on 2026-10-02:** a manager read `spawn.maxConcurrent` by hand **once**, on its pre-spawn cap check, and carried the value for two hours — so a target changed to **12** left it posting under-target cards against a remembered **18**, while its own drive leg *derived* the config from that memory rather than reading the file. A tick that skips this read is not comparing against the configured target at all. The script's own docstring is the incident's single home — read it there, never restate it here.
   **Two limits of this instrument, stated rather than hidden.** The ledger's own `status` is **not** a liveness source — measured 2026-10-01, 824 of its 1075 entries read `running` against 26 live registry sessions — so liveness must come from the two channels above and never from it. And a manager ever opened through `spawn_agent` would be counted, because it would hold a ledger record like any worker; managers are normally started by hand, which is why all four measured here had none.
   **Auto-resumes are excluded** — they answer to the auto-resume gate's own 30-min crash-loop cap, and counting them here would leave a sweep that revived two dead workers unable to start any new one. At the **soft** cap, open nothing further and print `⏸️ CONCURRENCY LIMIT: <n> running, <limit> allowed`; the remainder is picked up next sweep. At the **hard** cap nothing opens at all, and the refusal is carded rather than silent.
6. **Decide the mode before you spawn — `headless` only on positive evidence, and `interactive` whenever it is unclear.** The task's own `mode:` frontmatter is the storage: a spawn site reads it and passes the argument **whenever the field is present** — `interactive=false` for `headless`, `interactive=true` for `interactive` — and omits it only when the field is absent. ⚠️ **A non-task-anchored spawn — a bare brief with no task file — has no field to read, so it classifies the brief itself and passes the argument explicitly; it never omits.** Omission is reserved for a *task* whose `mode:` is absent, and that case is resolved upstream by Step 0.6 writing the field before the spawn. A bare brief has no such step, so omitting would mean the spawn carried **no decision at all** and reported `mode_source=config` — indistinguishable in the ledger from a site that never decided. Absent, classify the task's body now, write the field back with `vault-cli task set "<task>" mode <interactive|headless>`, and spawn accordingly — never re-derive over a value already on disk. **The classifying question is neither "is this dangerous" nor "can this run unattended".** The first is what a keyword grep measures (`kubectl`, `ssh`, `make apply`, `gh pr merge` say a task is *worth watching*, not that it *cannot run unattended*), and the second nothing in a task body reliably answers — `a routine task` names no infrastructure command and still needs a human. Ask instead: *does finishing this task raise questions mid-flight that only a human can settle?* Yes, or the body gives no basis to decide → **`interactive`**. `role: human` and `role: manager` both force `interactive` — a person needs a screen, a manager is a tab you jump to — while `role: agent` leaves the question genuinely open.

**What positive evidence looks like** — the task runs a fixed procedure end to end, decides nothing a human would want to weigh in on, and carries no approval step, no triage judgement and no "ask if unsure" in its own body. **Expect this to be rare.** Most recurring work triages, files, or decides something: an inbox sweep judges what is actionable, an alert check judges what to silence. Those are `interactive`, and a classifier that finds many headless rows is mis-reading the bodies, not finding an optimisation.

**The asymmetry is deliberate, and it is measured.** A wrongly-interactive task costs one idle tab the operator closes; a wrongly-headless one burns a whole session on gates nobody can answer (measured 2026-09-20: two workers stranded and four gates expired across three workers in ~90 minutes). The operator's own framing, 2026-09-21: *a session that needs interaction and is headless is hard to manage.* So `interactive` is a **floor, not a tie-break** — *unclear* is not a category that resolves to headless, and neither is *probably fine*.

⚠️ **A headless worker's two standing constraints, and both are the spawner's to carry, not the worker's.** (a) **Its gates park with the session that spawned it** — no other session can answer them, so the spawner must serve them via `await_permission` / `answer_permission`. A spawner that cannot answer a parked prompt must not open a headless worker, because the prompt will outlive it. (b) **Its turn ends at READY, and a turn end is not completion** — continue it with `spawn_agent(resume="<session-id>", interactive=false, cwd="<explicit>")`; a headless worker that has exited is neither finished nor restarted. Both constraints are why the `interactive` fallback is aggressive rather than polite.

⚠️ **`mode: interactive` is a resumability decision as well, and this is its only home.** The resume the auto-resume gate hands over is **path A** — `mcp__supervisor__spawn_agent(resume=…, cwd=…)` — and path A is **headless-only by construction**: its tab path launches the `cc-*` launcher without `--resume`, which is exactly what `resumeSupportError` refuses. So a task carrying `mode: interactive` is, **the moment its worker is orphaned, un-resumable by the mechanism the gate names.** The gate's clauses are all satisfiable on such a task, which makes it a gate that reports READY on an action that cannot execute. Precisely: the refusal fires whenever the **resolved** mode is interactive — always, once item 6 is honoured in both directions (the argument is passed as `interactive=true`), and equally under a `spawn.mode: interactive` config when the argument is omitted. Only a `spawn.mode: headless` config with the argument omitted escapes it, and that path violates the task's own declared mode — so it is not an escape.

Three consequences, all binding:

- **Never hand over a `To resume` row for a task whose `mode:` is `interactive`.** The drive leg reads `mode:` back off disk and stamps the row; a row its own mechanism cannot execute is not a decision, it is a stall, and the refusal that follows is terminal. Report it as `Not resumed` — the gate fails on this clause, and a gate failure lands there uniformly with the other clauses — naming `mode=interactive`, and give the operator the path-B recipe as the **manual** route.
- **`mode: interactive` is not a licence to fall back to path B.** Falling back is banned outright (see *A refused path is terminal*, below) — a refused path is a **second resume the gate never authorised**. Path B is the operator's manual route for a proven-dead session under a no-headless phase; it is not an automatic retry for a mode choice.
- **The classifier question in item 6 is unchanged by this.** `mode:` is chosen for how the worker must *run*, never for whether it can be *revived*; a task that will need resuming is not thereby `headless`. What changes is the manager's obligation: an `interactive` task's orphan is a **manual** recovery, and the escalation must say so rather than implying an automatic one exists.

⚠️ **A gate clause is the cheap fix here, not a resume path.** The alternative — teaching path A to carry a resume — collides with the standing ban above and its stated reason (path B "is a tab by construction, cannot produce a headless worker, and writes **no ledger row**"), and it is the deferred work of its own task. Until that lands, the honest behaviour is for the gate to **recognise** the case and escalate, never to hand over a row that cannot run.

7. **Decide the TARGET before you spawn — per-call, never from a config file; the cluster's URL and token are separate and DO come from one.** The target is **where** a worker is created; it is a different question from item 6's **which way it opens**, and it is answered by a different function. Omit the `target` argument for `local` — every worker this server opened before the argument existed: a WezTerm tab, or with `interactive=false` an in-process SDK query. Pass `target: "cluster"` to start the worker as a session **inside the `claude-interactive` service in nuke dev** instead, which additionally requires `task` (the vault task the session is bound to), a configured cluster URL, and the bearer token — the same value the service reads at startup, so one secret configures both ends. ⚠️ **Both resolve from either of two sources, env first: `SUPERVISOR_CLUSTER_URL` / `INTERACTIVE_AUTH_TOKEN` on the server entry, or a `cluster` object in the supervisor config file (`~/.config/claude-supervisor/config.json`).** ⚠️ **Prefer the file, and not for tidiness — it is the only source that reaches a server that is already running.** An MCP server's `env` block is read by Claude Code and **cached at session start**, so a value added to it reaches no running session by any in-session route: `/mcp` Reconnect re-spawns the child from that cached definition rather than re-reading the file. Measured 2026-10-04 — a server restarted three minutes *after* an env-block write still refused with *"no service URL is set"*. A file read by the **server** at its own start has no such problem, because a Reconnect re-execs the server and the file is read again. So the env route needs a **new session**; the file route needs only a Reconnect. ⚠️ **And a leftover env value SHADOWS the file** — precedence is env-first, so a half-migrated setup with `SUPERVISOR_CLUSTER_URL` still on the server entry and `cluster.url` in the file takes the *env* value, and the file looks like it does nothing. Nothing refuses in that state, which is what makes it quiet: **remove the env entry when you move to the file.** A cluster spawn with no token **refuses** rather than sending an unauthenticated request: the service answers a header-less `POST /prompt` with `401` *before* its route handler runs, so an unconfigured supervisor and a wrong token would otherwise be one indistinguishable observable. ⚠️ The header is only confidential over TLS — `SUPERVISOR_CLUSTER_URL` may be `http:`, and the NodePort shape is plaintext on the node network.

   ⚠️ **`cluster` is deliberately NOT a `SPAWN_MODES` value, and that is the whole design rather than an omission.** `SPAWN_MODES` is selectable from `SUPERVISOR_SPAWN_MODE` and `spawn.mode` in `~/.config/claude-supervisor/config.json`, so a third value there would let one config edit default the entire fleet into the cluster — and the cluster is *"a second option, not the default"*. The target is **per-call only**: `resolveSpawnTarget` has no env source and no config source, and that absence is deliberate.

   ⚠️ **An unknown target REFUSES the spawn rather than falling back to `local`.** Falling back is the failure, not the safe answer: a worker created on this Mac when the caller asked for the cluster looks exactly like a working call, and where a worker was created is otherwise discovered only by noticing it. Same rule and same reason as item 6's unknown-mode refusal.

   ⚠️ **A cluster worker still answers to item 5's limit — one value, two roles.** It is a worker like any other and must not become a second, uncounted population. ✅ **It is counted, since 2026-10-05.** `workerSessions()` reads the spawn ledger joined to the **union** of two liveness channels, and a cluster worker answers through the second one: `server/cluster-heartbeat.mjs`'s `pollCluster()` mirrors cluster sessions into the heartbeat store, which is the half the registry structurally cannot reach (that directory is pid-keyed and local-only). ⚠️ **This paragraph read *"it cannot be counted yet"* until then, and in doing so it forbade the fix** — recorded rather than quietly overwritten, because a reader who finds only the old text concludes the opposite of what the code now does. The same correction applies to the registry-only reading: a headless worker was uncounted for the same reason, and is counted now too.

   ⚠️ **`target: "cluster"` REFUSES `interactive`, `resume` and `policy` — it does not ignore them.** All three describe the local paths: `interactive` chooses between a WezTerm tab and a local headless worker, `resume` continues a conversation this server created, and `policy` answers permission requests raised here. The cluster path can honour none of them, and the tab path's own rule applies unchanged — an argument that would be dropped is **refused, not accepted and quietly ignored**, because a dropped `resume` looks exactly like a resume while opening a fresh session. So a cluster call site passes **`target` and `task` and nothing else** — note this is the one place item 6's "pass the argument whenever `mode:` is present" does **not** apply, since `mode:` is meaningless for a worker that is neither a tab nor a headless local query.

   ⚠️ **The returned session id is MINTED by the caller and is not proof the session exists.** The service's `POST /prompt` returns the session's *answer* as `text/plain` and echoes no id, because the caller supplies one (`X-Session-Id`). So a successful call shows the request was well-formed and a turn ran; it does not show which conversation ran it. The proof is the **serving pod's own log** — the `turn start id=<id>` / `turn end id=<id>` pair — and a caller that treats the response as the evidence has an unfalsifiable check.

8. **Decide the `cwd` and the vault before you spawn — neither is defaulted any more.** `spawn_agent` requires one of **`vault`** (the vault name — it resolves the worker's directory **and** that vault's `claude_script` from one value, so the two cannot disagree; this is the form to prefer) or **`cwd`** (which must resolve to a configured vault by containment, longest matching path first, so a nested vault is not shadowed by the one containing it). Passing both is allowed only when they agree.

   ⚠️ **Omitting both used to default to this server's own working directory, and that is precisely what the 2026-10-03 incident was.** A manager session called `spawn_agent` **27 times** with no `cwd`: every worker started in `~/Documents/workspaces/claude-supervisor/server` and every one ran **Opus** instead of the vault's `cc-private-deepseek`. The launcher lookup fell back to a vault named `personal` — renamed `private-personal`, so the entry matched nothing — and then to the bare `claude` binary; and the response named neither the launcher, nor the vault, nor the model, so the caller had no way to see any of it. A wrong-cwd, wrong-model worker returned exactly what a correct one returned.

   ⚠️ **All four failures are refusals now, not fallbacks** — the same rule and the same reason as items 6 and 7: no `cwd` and no `vault`; an unknown vault; a vault with no `claude_script` (or a `cwd` whose vault has none); and a `cwd` outside every configured vault. A `cwd` and a `vault` that disagree are refused rather than one silently winning. `SUPERVISOR_CLAUDE_CMD` stays the top launcher override, because it is an explicit operator setting rather than a fallback.

   ⚠️ **The spawn response reports `vault`, `launcher` and `model`**, so all three are verifiable from the reply rather than by opening a pane and reading a status line. The model comes from the launcher script's own `--model` argument, with a `${VAR}` resolved against that script's `export`; it is `null` when the script cannot be read, never a guess — a plausible wrong model would defeat the field's whole purpose.

⚠️ **This block is the one authoritative home for the rule.** Every spawn site references it rather than restating it — the fleet command, the fleet runbook, and the manager-loop command all point here. It owns **six constants**: the **readiness ladder** (item 2 above — all four of its branches, its thresholds, and the `UNFIXABLE:` handling), the **fleet-wide concurrent limit** (item 5 above), the **mode decision** (item 6 above — the classifier, the `mode:` field, and both headless constraints), the **target decision** (item 7 above — `local` vs `cluster`, why `cluster` is per-call only, and why the returned id is not evidence), the **cwd/vault/launcher decision** (item 8 above — why neither is defaulted, and what the response reports), and the **repair dispatch** (§ *The repair dispatch* below — who repairs a `🔧 Repairable` row, and the per-round cap). Each appears once, there, and is referenced everywhere else. ⚠️ **A restated copy of any of them is not a harmless comment — it is a second counter.** The cap was restated in `commands/manager-loop.md`, `commands/manager-verify.md` and the manager runbook's Guardrail 2 until 2026-09-24: four homes for one number, which is how a single cap becomes two caps the day one home is edited and the others are not, with no error and no diff to catch it. The mode rule carried the mirror-image defect until the same day: it lived in `commands/open.md` § Step 0.6 and was consumed only there, so every other spawn site silently fell through to the fleet config — measured 2026-09-23, **63 new-worker spawns in one day and 0 of them headless**, 57 sourced from `config` rather than from any decision.

**A — `spawn_agent` (the preferred mechanism, within `/open`).** The prompt is a spawn *argument*, so the task never goes
over keystrokes:

```
mcp__supervisor__spawn_agent(prompt="...", vault="private-personal", label="alpha", role="agent")
```

⚠️ **`interactive` is resolved from the fleet's config — omit it.** With no argument the
server resolves, highest first: `SUPERVISOR_SPAWN_MODE` → `spawn.mode` in
`~/.config/claude-supervisor/config.json` → its built-in `interactive`. **Do not pass it on
a fresh spawn.** Passing it is what made the config unreachable before 2026-09-18, when each
manager command file hardcoded `interactive=false`, so changing the fleet's mode meant
editing N instruction files and course-correcting every manager already running (measured
that morning: 3 workers killed, 7 more found under two other managers). The file is read
once at server start — restart the MCP server after editing it. The spawn response reports
`mode_source` (`argument`/`env`/`config`/`default`) when you need to know which source
decided. Pass `interactive` as a per-call override in one case only: `true` to watch one
worker's screen live.

⚠️ **The `mode:` case is a carve-out, not an override, and it runs in both directions.** Pass the argument whenever the task's `mode:` field is present — `interactive=false` for `headless`, `interactive=true` for `interactive` (item 6 above). An absent `mode:` still means *omit the argument entirely* — never *pass `interactive`* — so the config keeps deciding for every task that has not opted out, and the one-place-to-change property survives. **If this file writes the field, this file must honour it in both directions.** Passing it only for `headless` — which is what this file did until 2026-09-24 — leaves `mode: interactive` inert: a task that explicitly declared itself interactive flips to headless the moment `spawn.mode` moves, and the omitted argument reports `mode_source=config`, so a **wired spawn becomes indistinguishable from an unwired one in the ledger**. **A `mode:` honoured in one direction is the deleted mode column wearing a different hat.**

⚠️ **A headless worker is told its own mode — it never has to infer it.** The resolved mode
and its source are passed into the worker's environment as `SUPERVISOR_WORKER_MODE` and
`SUPERVISOR_WORKER_MODE_SOURCE`, so the worker reads them instead of reading the fleet
config. That matters because the config describes the **fleet, not this worker**: a per-call
`interactive: false` opens a worker headless while the file still reads `interactive`, and
before 2026-09-20 that file was the only signal a worker could reach — so every
headless-by-override worker mis-modelled itself, reported it was in an **interactive tab**,
and waited for a keystroke that could never be typed (measured 2026-09-20: two in one hour,
one ending `done`/`success` with its task file unedited). A headless worker is an in-process
SDK `query()` with no pid and no argv, so it cannot probe this for itself: the value is
handed over, never re-derived. ⚠️ The SDK's `env` option **replaces** the subprocess
environment rather than merging, so `process.env` is spread explicitly — without it the
worker loses `PATH`, `HOME` and `ANTHROPIC_BASE_URL`, the last of which stops it routing
through the router while looking like nothing at all.

⚠️ **`role` resolves BOTH the colour and the window — pass it, and prefer it over
`window_id`.** The server reads the map the WezTerm config publishes on its reconcile tick
(`~/.cache/wezterm-role-map.json`) and resolves `manager` → orange/Managers, `agent` →
pink/Agents, `human` → cyan/Direct. **Omit it for an agent**, which is the correct default
for every task that has not declared a role.

```
mcp__supervisor__spawn_agent(prompt="...", cwd="/path", label="alpha", role="manager")
```

**A role is a WORD, and that is the whole point.** A `window_id` has to cross the MCP tool
boundary, and `window_id: 0` did not survive that crossing reliably: measured 2026-09-20 it
reached the server 4 times in 6 and silently inherited the caller's window the other times —
both failures were a run's first spawn, and no reproducible trigger was found. A role cannot
be dropped that way, and the id is looked up in-process at the moment of spawn. The CLI
itself is not implicated: `wezterm cli spawn --window-id 0` from a shell landed in window 0
six times out of six.

The spawn response reports the resolved `role` and `window_id`, so routing is **observed**
rather than inferred from wherever the tab happened to land. An explicit `window_id` still
wins when passed — reach for it only for a window the role map does not describe.

The two failure modes are deliberately different. An **unusable map degrades**: absence is
normal, since the map publishes on a reconcile tick and a headless worker has no window at
all, so the spawn proceeds on the caller's explicit window and `SUPERVISOR_WORKER_COLOR`,
with a warning logged. An **unknown role is refused**, because it is a caller mistake and a
worker opened with the wrong colour is discovered only by noticing it.

`SUPERVISOR_WORKER_COLOR` remains an explicit operator override and wins over the resolved
chip; `off` means send no colour. There is deliberately **no built-in colour default** — the
colour is a role signal, so a hardcoded one is the defect it replaced.

**Fresh start — the worker creates its own session.** Spawn with the work command as the
`prompt` argument and **no pre-minted `session_id`**:

```
mcp__supervisor__spawn_agent(prompt='/vault-cli:work-on-task "<task>"', vault="<vault>", task="<task>", label="<task>")
```

The worker runs its own planning turn, in its own pane. **Never mint the session first:**
`vault-cli task work-on "<task>" --mode headless` runs that turn inside the *caller's*
session — blocking it for minutes — and the `session_id` it returns is the value the very
next documented step consumes. Measured 2026-09-19: exit 124, an empty output file, and pids
still running after the caller had given up. With `interactive` omitted the config decides,
and on the fleet's current setting the worker lands in a real tab, so its progress is
visible.

**A fresh start needs no `session_id`; a resume cannot work without one.** A resume continues
an existing conversation, so it must name which one; a fresh start has no prior conversation
to name, and the worker mints its own. That asymmetry is why the two paths diverge below.

`policy` is headless-only, which is why a tab-producing call site never carries it.

The launcher is resolved from `vault-cli config` (`claude_script`) for the vault the worker
belongs to — named by `vault`, or found by containment from `cwd` (item 8 above). There is
**no fallback**: an unknown vault, a vault with no `claude_script`, and a `cwd` outside every
vault are each refused. The fallback this sentence used to describe — the `personal` entry —
was a vault that no longer exists (renamed `private-personal`), so it matched nothing and the
launcher silently became the bare `claude` binary, which routes around the router, the MCP
config and the model selection. Never invoke that binary. Override with
`SUPERVISOR_CLAUDE_CMD`.

**Precondition, and it fails silently:** `supervisor` must be in the launcher's MCP config
allowlist. Launchers that pass `--strict-mcp-config` exclude plugin-provided MCP servers —
the plugin reads enabled and `claude mcp list` reports healthy while the tools are absent
from every session. If `mcp__supervisor__*` is unavailable, use path B.

**B — raw `wezterm cli spawn` (fallback, and the path for resuming a live session).**

```bash
wezterm cli spawn ${WINDOW_ID:+--window-id "$WINDOW_ID"} --cwd "<cwd>" -- bash -lc 'unset CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CODE_MESSAGING_TOKEN CLAUDE_CODE_SESSION_ID CLAUDE_CODE_CHILD_SESSION; exec "<claude_script>" --resume <session_id> -n "<title>" "/color '"$CHIP"'"'
```

**`--cwd` is required, and a resume does not inherit it.** The session id names a
conversation, not a working directory, so `wezterm cli spawn` falls back to *wezterm's own*
working directory — `$HOME` for a server started from a home directory. Claude Code then stops
on *"Accessing workspace /Users/&lt;user&gt; — do you trust this folder?"* and registers **no pid
at all**, so a resume that worked perfectly reads as a no-op: a caller checking the registry
finds nothing and reads "did not take", a caller checking the exit code reads "took", and
neither reading names the trust dialog. The value comes from the session registry —
`~/.claude/sessions/<pid>.json` carries a `cwd` field (verified 2026-09-24: 26 of 26 records on
this host) — and is resolved **before** the spawn, never guessed or defaulted: a session whose
record carries no `cwd` is refused rather than resumed into the wrong tree. Path A states the
same requirement for `spawn_agent` at item 3 below; this is its path-B twin, and the two must
not drift into two different resolution rules.

**`$WINDOW_ID` and `$CHIP` are a role-resolved PAIR**, read from the published map before
spawning (`~/.cache/wezterm-role-map.json`): `manager` → Managers / orange, `agent` →
Agents / pink, `human` → Direct / cyan, with `role:` absent resolving to `agent`. Both are
role signals, so a hardcoded colour or a missing `--window-id` **is** the defect this shape
was corrected for on 2026-09-20 — the old `"/color pink"` painted a manager as a worker, and
the missing flag dropped the tab into whatever window the caller happened to occupy.
`${WINDOW_ID:+…}` keeps the flag off entirely when the map is unavailable, so the fallback
degrades to the old behaviour instead of passing an empty `--window-id`.

Two load-bearing details:

- **Keep the `unset`; its reason is NOT established.** The peer-registration explanation was
  refuted by experiment. It is harmless and cheap, so it stays — do not restate the socket
  story as its justification.
- **The colour goes in the spawn; the work command is typed afterwards.** Claude Code parses
  one submitted message as one command, and `/color` takes the *entire* trimmed argument, so
  seeding it inside a larger prompt yields `Invalid color`. Type the work command after:
  `wezterm cli send-text --pane-id <N> --no-paste $'<work command>\r'`. The trailing `\r` is
  load-bearing; a bare `\n` leaves the text unsubmitted.

⚠️ **Typing is legitimate only into a pane this recipe just spawned, while it is still idle.**
Answering another session's operator gate by `send-text` is forbidden. Typing into a pane
showing `Enter to select` turns any keystroke into a menu selection.

**Why A is preferred over B:** A removes the typed *task*; B only hardens the typist. **B is a
fallback, never the default** — it types into a pane and steals focus, and it has **no target at
all for a headless worker**. Reach for it only where A cannot do the job: a resume of a session
whose liveness cannot be determined, or a resume where headless is not permitted for the phase.
`send_agent_message` is not an alternative channel — it types and steals focus. Two limits on
A: it can only supervise sessions **it created**, and `spawn_agent({resume})` refuses a
session that is still live. Measured 2026-09-18: a call site that omitted the follow-up
`send-text` left a worker (BRO-21462, spawned 09:03) idle for 16 min while its task file
stayed untouched.

⚠️ **The `resume` refusal is a property of `mcp__supervisor__spawn_agent`, not of the
platform.** `resumeSupportError` (`server/tab.mjs`) rejects `resume` alongside
`interactive:true` because the *tool's* tab path launches the `cc-*` launcher without handing
it `--resume` — the flag would be dropped and you would get a fresh conversation while
believing you were continuing one. Path B hands `--resume` to the launcher directly, so it is
**interactive and resuming at once**. Read the refusal as *"this tool's tab path cannot carry
a resume"*, never as *"a resume cannot be interactive"*.

**Resume takes two independent decisions — first the path, then the drive.** Liveness picks
the path; *why the session died* picks whether anything drives the resumed pane. They do not
substitute for one another.

**1 — Path, by liveness:**

| Resuming… | Path | Why |
|---|---|---|
| a session **proven dead** | **A**, `interactive=false, resume="<id>"` — or **B** where headless is not permitted (below) | A is headless, so prompts park for the manager. On A, `resume` **requires** `interactive:false` — the pair is refused there, not silently downgraded |
| a session whose liveness **cannot be determined** | **B**, `wezterm cli spawn --resume` | A refuses an unverifiable resume by design |

⚠️ **Where headless is not permitted, the proven-dead row moves to B.** Some phases forbid
headless fleet-wide — the operator's standing constraint during manager-system development:
*"we are currently not running headless ever because we are in the development of the manager
system."* That constraint is **phase-scoped, not permanent**; do not delete path A on account
of it. Under it a proven-dead session resumes via **B**, exactly as an indeterminate one does.
The row's preference for A is a preference, not a requirement, and it is the only row affected.

⚠️ **A refused path is terminal — never fall back to the other one.** If the chosen path's spawn
is refused, the resume did not happen: report the refusal and stop. Falling back from B to A (or
A to B) is not a retry — it is a **second resume the gate never authorised**, and it opens a
worker on a path the gate rejected, under a stamp that now records a resume that never took.
The refusal is the safe direction and it is the design: path A already refuses `resume` +
`interactive:true` rather than silently downgrading it, and a refused resume costs one sweep
where a wrong-path spawn corrupts a conversation.

**2 — Drive, by cause of death:**

| The session died… | Then |
|---|---|
| **mid-work** | the resumed pane lands at an empty composer with nothing driving it — **deliver the work**: as the `prompt` argument on A, or typed after the colour on B |
| **holding a human gate** | **drive nothing.** The pane comes up holding its own unanswered gate; idling there is the *correct* state |

⚠️ **A resume onto an unanswered operator gate must never be driven.** The two cases are
indistinguishable from the session id alone — read the transcript tail before choosing. The
error is not symmetric: an undriven mid-work resume wastes a session, a driven gate-death
resume **answers a question on the operator's behalf**.

**The discriminator is the transcript tail, and the pane then vetoes.** The tail decides the
branch: its last `👤 You:` line classifies, and a closer that is absent, `nothing`, or a parked
`later (on …)` means **mid-work** — nothing is waiting on a human. Only a real `pick` /
`approve:` / `you run:` / `review:` line is **gate-held**.

⚠️ **There is no third branch for the restart-request death, and none is needed.** The
2026-09-20 case — a session that died holding a gate that was its own restart request,
satisfied by the restart itself — lands in **mid-work** by construction: a harness
`restart Claude Code` gate is not a `👤 You:` line, so the tail reads as "nothing waiting". Do
not add a restart-detection heuristic to catch it; the classifier already agrees.

⚠️ **The pane text vetoes the tail, and the veto wins.** Read the resumed pane before typing
anything. A selection modal (`Enter to select`) or a non-empty composer means **type
nothing** — a keystroke into a modal selects a menu option, and a resume landing on one is
exactly the state the 2026-09-20 run misread as a live gate. The signals can disagree: the
tail is what the session *wrote*, the pane is what it is *holding*. Report the veto and hand
over the pane instead of answering for it.

Both halves are implemented in `scripts/restart-precheck.py` (its `classification:` line),
with the orchestration in `commands/worker-restart.md`. Do not restate the classifier
elsewhere — two copies of a transcript-tail read drift silently, because both keep returning a
string.

**3 — `cwd` (or `vault`) is not inherited on a resume — pass it explicitly.**

`spawn_agent(resume="<id>", interactive=false, vault="<vault>")`. The resumed session starts in
the directory you name, **not** the directory the original session ran in: the session id names
a conversation, not a working directory, and nothing carries the old one across. A resume that
names **neither** `cwd` nor `vault` is **refused** (item 8), not defaulted — so a worker
resumed to finish repo work can no longer silently start somewhere else and send its first file
operation to the wrong tree, but a caller who never had the directory still has to go and find
it. Read `cwd` from the session registry (`~/.claude/sessions/<pid>.json`) rather than
guessing; that record is the only place the original directory survives.

**Confirming a headless spawn took — and what to read instead of `.status` — is owned by
§ A headless worker exits at turn end.** Do not restate it here: a restated copy is what let
four surfaces prescribe the same wrong field.

### The repair dispatch — who acts on a `🔧 Repairable` row

`agents/manager-drive.md` clause (1) classifies every non-terminal row in the caller's tracked set and returns exactly one verdict per row. `🔧 Repairable` is one of them: it names a row below the readiness ladder whose task file needs a **structural repair** before it can clear. ⚠️ **The leg cannot perform that repair, and this is deliberate rather than a gap** — it holds no `Edit` and no `Write`, and its `Task` tool is restricted by its own `<constraints>` to the read-only `task-auditor`. Clause (1) states the split outright (*"The write is the caller's"*), and `commands/open.md` § Step 1.5 says the same of the improvement pass.

**The repair actor is the caller — the session that dispatched the leg.** Two sites carry it, and no others:

| Site | Path | When |
|---|---|---|
| `commands/manager-loop.md` step 4's `Act:` block | the loop | once per tick |
| `commands/manager-drive.md` step 7 | by hand | in the same step that already executes the leg's `To open` rows |

Each runs `/supervisor:ready "<task>"` **once per *selected* `repairable` row**, bounded to the round's `repairable` set and **capped at `REPAIR_MAX_PER_ROUND` = 3 rows per round**.

⚠️ **A cap over an unordered set starves its tail, so the selection order is part of the rule: score ascending.** On this change's own measured basis — **12 `repairable` rows, five of them at score 6, against a cap of 3** — an unranked set would dispatch the same three 6s on every round and never reach the 7s or the 8s, reproducing for the tail the exact *"a verdict set that no one acts on"* defect this section exists to close. **Order the round's `repairable` rows by score ascending — worst structural gap first — and take the first `N`.** This is clause (7)'s third rank (*"the rest, by score"*) applied to an act rather than a card, and the ordering is what makes the tail advance: a row repaired past the ladder **leaves the set**, so the next round's window moves down the ranking. Rows beyond the window are printed `left for the next round (cap <n>)` — never silently dropped, because a silent cap is indistinguishable from a set that was fully processed.

⚠️ **The residual, named rather than hidden: a row that cannot be repaired keeps its low score and therefore keeps its slot.** The bound on it is clause (1)'s own escalation — a row that survives the loop without clearing is carded and held — but nothing today stops the caller re-dispatching that row on the next round, because the verdicts cache records a row's `content_key` and its verdict, not *when it was last dispatched*. Closing that properly needs a per-row dispatch cursor (or a rotation offset) in the manager-predispatch store; until it exists, a permanently-`repairable` row can hold one of the cap's slots. ⚠️ **That is a known, filed gap — not a claim that the cap is starvation-proof.**

⚠️ **The cap is bounded by the fleet's remaining capacity too, not by 3 alone.** `/supervisor:ready` can **open, and therefore spawn** — `commands/ready.md` step 4 hands to `/supervisor:open`. So a tick dispatches **`min(REPAIR_MAX_PER_ROUND, target − live)`** repairs, reading `live` and `target` with the same instruments the tick's own cap check uses (item 5's script and its target read), never from memory. **At or above the target it dispatches zero** and prints `repairs held: at target (<live>/<target>)` — the same discipline the under-target branch carries, so a suppressed tick never renders like one that found nothing.

⚠️ **The dispatch has two consequences, and only one of them is bounded above — name the other.** `/supervisor:ready` **opens** (→ spawn, bounded by the capacity line just given) **and it cards**: `commands/ready.md` step 4's second branch, and `agents/manager-drive.md` clause (1) step 2's below-bar terminal branch, both post an **attention-board card** for a row still below the ladder after the pass. So one round can emit up to the cap in cards as well as in spawns, and nothing in the loop counts the cards. ⚠️ **It is bounded per row by `task_identifier` dedup** — the below-bar card's key is the task's identifier, so a row carded on one round is not carded again on the next — which is why this is **named rather than capped**. Named, though, and not left implicit: a section that bounds the spawn side while staying silent on the card side reads as though the spawn were the only consequence, which is exactly the kind of unstated half this file's "both consequences binding" standard exists to catch.

⚠️ **Re-read `live` immediately before the repair dispatch — never once per step.** Both sites run the leg's `To open` rows first, and **each of those can spawn**, so a `live` read taken at the step's cap check is stale by exactly the `To open` count and the bound over-admits by that many rows.

⚠️ **And this caller-side bound is a courtesy, not the enforcement — say so plainly, because the misreading runs in the unsafe direction.** `spawn_agent` enforces the limit itself: `concurrentLimitError()` (`server/supervisor.mjs:741`, called on the spawn path at `:962` and the cluster path at `:828`) re-resolves `maxConcurrent`, re-reads the live count, and refuses **both** when `live >= limit` **and** when the count cannot be read at all. A stale or optimistic `live` here therefore degrades to a **refused open that the next sweep picks up** — never to an overrun. ⚠️ **Never describe this bound as what keeps the fleet inside its limit, and never drop the server check on the strength of it:** a reader who believes the caller bound is load-bearing may drop the re-read rather than notice the refusal already covers it.

⚠️ **The repair half itself is not restated here.** `/supervisor:ready` already runs the audit → repair → re-audit loop for one named row (`commands/ready.md`), and `agents/manager-drive.md` clause (1) owns that loop's round cap, its stop condition and its early-stop-on-repetition rule. This section owns only **who dispatches it** and **how many per round** — a second copy of the loop is the second counter the block above forbids.

⚠️ **The cap is a bound, not a budget.** Rows beyond it are reported as `left for the next round (cap <n>)` — never silently dropped, because a silent cap is indistinguishable from a set that was fully processed, and the whole defect this closes is a verdict set that no one acts on. ⚠️ **Never dispatch a `phase: todo` row:** `/supervisor:ready` refuses one at its Gate 1, and that refusal is the operator's approval boundary rather than a defect to route around.

⚠️ **Do not close this gap by giving the leg a write tool.** The split is load-bearing: the leg decides, the caller acts. A leg that could write would be a second writer on task files its own caller is editing in the same tick.

⚠️ **Measured basis — 2026-10-03, tick 60 of the `Manager Layer` topic manager.** The leg returned **12** `repairable` rows, scored and cached, and repaired none; the same 12 had been scored the tick before and the tick before that. Because the verdict is cached by `content_key`, a re-scored `repairable` row costs no further audit — so nothing in the loop ever noticed that its verdict had been acted on zero times.

### The tab name is the join — and it is made unique before the process starts

A tab worker is a separate process the supervisor does not create, so it never learns that
worker's session id the way it does for a headless one. The registry carries `name`, and the
spawn sets the tab title, so **the name is the only join back from a tab to its session id**:
`findRegisteredByName("⚙ " + label)`, polled for up to 8s after the pane opens.

A name is not unique, and `find` returns the FIRST entry matching it. A label reused while an
earlier worker still answered to it therefore resolved the new spawn to **that** worker — and
the old session's id is what went into the new worker's ledger record. So the name is derived
*before* the process starts: `uniqueTabName` suffixes `(2)`, `(3)`, … until nothing holds it,
and the poll carries the holders snapshotted before the spawn as an `exclude` set, so a name
taken in the race between that snapshot and the poll cannot be matched either.

Two consequences worth knowing when reading a roster:

- **A suffixed tab title means the base name was taken.** A worker spawned under a label
  already in use comes up as `⚙ <label> (2)`, not `⚙ <label>` — the suffix is the guard
  working, not a naming mistake.
- **`sessionId: null` on a spawn response is the honest "never registered".** The poll could
  not resolve the name to a session that did not already exist, so no ledger record is
  written for it — `writeLedger` skips a falsy id and logs `WARNING: no ledger record for
  <id>`. A spawn **can** return `sessionId: null` while still opening a working tab.

⚠️ The guard is on the **name**, not on one cause of a collision: it makes the join
unambiguous whatever produced the earlier holder, including a cause not yet identified.

## A headless worker exits at turn end — that is not "finished"

Two exits look like completion and are not. A headless worker **ends its turn on a READY
panel**, and a parked question **times out (~11 min)**, after which it exits with the question
unanswered. Neither is an error, and neither leaves a trace beyond the roster row disappearing.
**A headless worker that has exited is a parked process, not a finished one** — read it as
"waiting for a continuation", never as "done".

The continuation is a new turn, not a restart:

```
mcp__supervisor__spawn_agent(
  prompt="<the answer, or the next instruction>",
  resume="<session-id>",
  interactive=false,
  cwd="<explicit — see above>",
)
```

The prompt is a **plain user turn** — no relay prefix, no provenance wrapper. That is what
separates continuing a worker from answering one:

| The worker is… | Whose answer | Channel | Call |
|---|---|---|---|
| **still parked** on a question | the **operator's** | the permission channel | `answer_permission(deny, message="Operator answer, via supervisor: <option>")` |
| **still parked** on a question | the **manager's own** | the permission channel | `answer_permission(deny, message="Manager answer, via supervisor: <option>")` |
| **already exited** (timeout, or turn end) | either | a fresh turn | `spawn_agent(resume=<id>, interactive=false, cwd=<explicit>)` |

They do not substitute for one another: `answer_permission` cannot reach a process that has
exited, and `spawn_agent(resume=…)` cannot answer a question that is still parked. Decide which of the two applies from `agent_status` **plus the transcript's mtime** — never
from `agent_status` alone, because `.status` is terminal only once the turn has ended
(`num_turns: 0` beside `subtype: "success"` is the tell that it has not).

### The two prefixes are not interchangeable

A parked gate can be answered in one of two voices, and the prefix is what declares which. Both
are honoured **on an `AskUserQuestion` only** — a denial on `Bash`, `Edit` or `Write` stays a
denial whatever prefix it carries.

- **`Operator answer, via supervisor:`** makes a **provenance claim**: the operator answered this
  question, in the manager session, in the current exchange. Use it only when that is literally
  true. A manager deciding on its own under this prefix is **forging an operator answer**, which
  is why the worker-side rule is written to reject it — measured 2026-09-20, a worker that
  received a manager's own inference under the operator form returned `ANSWER=NONE` and did not
  act, correctly.
- **`Manager answer, via supervisor:`** makes **no provenance claim**. It is the manager's own
  decision, on a question the manager owns — a choice between alternatives, not a question that
  was ever the operator's to answer.

⚠️ **Neither prefix releases an irreversible or production-touching action.** Those still need
the operator's own confirmation, obtained directly. A relay launders precisely the thing that
makes such a confirmation worth having — the operator's own wording naming the target and the
command — so a manager-prefixed denial that names one must leave the gate unanswered. This is
stated as a rule rather than a preference because it is the one branch where getting it wrong is
unrecoverable.

⚠️ **Why a parked gate needs the prefix channel at all.** `resume` refuses a session that is
still running, so the fresh-turn channel reaches only an **exited** worker. A parked gate is a
live one, and before 2026-09-21 a manager had no honest way to answer it in its own voice: the
only documented prefix asserted operator provenance. See
the recorded diagnosis of that gap.

⚠️ **A worker that exited on a question timeout still holds an unanswered question.** Its exit
does not answer it. Resuming with an empty prompt, or with "continue", leaves the question
unanswered and the worker parked again at the same gate; the continuation prompt must carry the
operator's actual answer, exactly as `answer_permission` would have.

## The session roster — `fleet-sessions.py`

`scripts/fleet-sessions.py` prints one row per session transcript on the machine, newest
first. It is an **input** to the sweep table above, not the sweep table itself: the manager
commands join it to `ListAgents` on the session id and re-render. The roster is always every
project, with no scope to pass.

```
LAST-ACTIVE  PROJECT      LIVE SESSION    WORKING ON                              SPAWN MODE  ATTRIBUTION
7h ago       another vault  ●    dfa12e37   a sample task…    headless    a sample task…
1s ago       my-vault          46647e0e   Show Spawn Mode and Fleet Attribution…  interactive Show Spawn Mode and Fleet …
```

- **Columns, in this order:** `LAST-ACTIVE · PROJECT · LIVE · SESSION · WORKING ON ·
  SPAWN MODE · ATTRIBUTION`. The first five keep their positions and widths — the manager
  commands parse `SESSION` (the join key) and `WORKING ON` by name against them, so the two
  new columns are **appended**, never interleaved.
- **`SPAWN MODE` and `ATTRIBUTION` come from the spawn ledger**, joined on the session id.
  The ledger is the only store recording the spawn edge, so without it a headless worker and
  a human tab render identically.
- **Ledger directory:** `SUPERVISOR_LEDGER_DIR`, else `$XDG_STATE_HOME|~/.local/state` +
  `/claude-supervisor/sessions` — resolved from the writer's own override
  (`server/config.mjs`), never hardcoded. ⚠️ Deliberately **not** `SUPERVISOR_SESSIONS_DIR`,
  which names the live registry (`~/.claude/sessions`) — a different store with a different
  lifetime.
- **`unknown` is a value, not a blank.** A session with no ledger record was never
  *recorded*, which is a different claim from *not spawned*. Both new cells render the
  literal `unknown`; a blank would collapse the two and read as a value the ledger supplied.
- **`ATTRIBUTION` is `parent_session` + `label` when both are present**, falling back to
  whichever exists. ✅ Fixed 2026-09-22: `parent_session` now resolves to the nearest
  registered ancestor, so the column names the manager for records written since. ⚠️ The
  store's **existing** records keep `null` — measured that day at 398 of 398, and they are
  deliberately not backfilled, because a repaired edge would assert an attribution nobody
  observed at spawn time. So a label-only cell means *the manager was not recorded*, not
  *there was no manager* — and since the two vintages are indistinguishable by field alone,
  compare `spawned_at` against the release that added the ancestor walk before reading a
  `null` as a defect in the current build.
- **Two spawn counts, both labelled:** `spawned today (UTC): N` and `spawned today (local): M`.
  `spawned_at` is UTC-only, so the day boundary has two defensible readings and the view
  states which is which rather than silently picking one. They legitimately differ by the
  records straddling the boundary.
- ⚠️ **No count and no column rests on `status` / `ended_at`.** Those never close reliably
  for any mode (see the runbook's Step 7), so they are not a liveness source and are not
  read here. Every count is over `spawned_at`.
- **Retired flags stay no-ops.** `--all`, `--minutes N` and `--vault NAME` are accepted and
  ignored — the roster is always every project.

## Sweep output — the fleet table

**The frame is owned by the Fleet Manager Session runbook (per-vault) § Sweep output — the
fleet table, and this file deliberately does not restate it.** The columns, the widths, the
icons, the tree layout in the Session column, the marker line and the action lines are all
specified there; `/supervisor:fleet-loop`, `/supervisor:fleet-status` and this file point at
that one section rather than carrying a second copy. Two files each claiming to be the single
source is exactly the drift this pointer removes — measured 2026-09-24, both claimed it.

What this file still owns is the part that is about the plugin rather than the frame:

- **Build the rows with `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/fleet-board.py --json`, render them with the same root's `scripts/box-table.py`** —
  stdin `{"header": [...], "rows": [[...]], "widths": [...]}`, with the board's extra keys
  (`counts`, `sessions`, `residual`, `coverage_ok`) ignored by the renderer. **Never hand-draw the box.**
  ⚠️ The board **asserts its own coverage** and exits non-zero rather than printing a table
  that omits a session — one row per registry entry, plus every transcript-fresh session the
  registry carries present among the rows, plus every session row present **exactly once** in
  the drawn tree. A **residual** line reports transcript-fresh sessions the registry does
  *not* carry (a headless worker holds no registry entry at all). Never read a short table as
  a clean fleet, and never read an empty one as an empty fleet.
- **`--json` carries one entry per session under `sessions`** — `session_id`, `label`, `role`
  (`manager` / `worker` / `unmanaged`), `parent` (a session id, or `unmanaged`) and `bucket`.
  The tree is drawn from `parent`, so a consumer that needs the structure reads the document
  rather than parsing the glyphs.
- **The bucket rule's single source is `scripts/fleet-board.py` itself** — its module
  docstring defines the four buckets and the precedence that makes the classification total.
  Neither this file nor the runbook restates it.
- ⚠️ **Do not type a leading glyph.** The harness already bullets assistant output with `⏺`;
  a literal copy renders doubled.
- Below the box, only the non-empty action lines: the **blocked-by-you jump list**,
  `⚠️ ORPHANED: <task> — <why>`, and `⚠️ ACTION NEEDED: <the human decision>`. **Names lead**;
  the `[ref]` and pane id are secondary. Each `jump:` target is the one-line output of
  `scripts/jump-link.py <PANEID>` — a clickable `http://127.0.0.1:1337/jump?pane=<N>&t=…`
  link when the local fleet-jump server is configured, and the `/supervisor:jump <N>` command
  when it is not. **Never hand-write the URL**: the token lives in a 0600 file outside every
  repo, so a hand-built link is either broken or leaks it. ⚠️ **Hand over a pane id, never a
  tab id** — a tab that moves windows is renumbered, so a handed-over `--tab-id` goes dead
  (measured 2026-09-18: tabs 158/159/160 in window 0 became 163/164/165 in window 2, and
  `activate-tab --tab-id 159` failed outright while `activate-pane --pane-id 239` worked
  immediately). `/supervisor:jump` stays the executor behind the link; a raw
  `wezterm cli activate-tab` line is not a handover.

The table is the dashboard; TTS stays problem-only and voice-mode gated; the action lines
appear only when non-empty.

## Session stamps

A task's `claude_session_id` names the session working it, and a session answers "what am I
working on" by resolving that stamp. Reading a stamp as ownership — whose task it is, and when a
foreign one blocks you — is a rule recorded separately. This section is the
multiplicity rule: how many stamps one session may carry.

- **A session may carry many stamps, but at most one on an open task.** A session that finished
  task A and moved on to task B legitimately stamps both, and the stamp on A records who did the
  work, so it is **never rewritten to clear a count**. Two *open* tasks behind one stamp are an
  ambiguous answer to the resolution question, and that is the defect.
- **Task creation never stamps.** The session that works a task stamps it, not the session that
  wrote the file. Session-connect writes a stamp only when the field is empty, so a creator's
  stamp is never replaced by the real worker's — the worker never takes ownership.
- **A manager never stamps a worker's task.** Managers route work; they do not claim it.

Measured 2026-09-25 over `25 Tasks/`: 33 stamps spanned 2+ files. Zero were manager stamps; 30
were one ended session's finished tasks in turn (permitted history); 3 were a creating session's
stamp on an open task, one of them later worked by a different session whose id the task never
took. Counting "2+ files" called all 33 defects. The rule calls none of the 30 a defect.

`scripts/stamp-check.py <tasks_dir>` applies the rule. It exits 1 on a **violation** (one stamp on 2+
open tasks) and lists a **suspect** without failing: a stamp on one open task whose
`metrics_sessions` names other workers but not the stamp. `/supervisor:fleet-verify` check 3 reads
it. The field is read from the frontmatter block only, and an empty `claude_session_id:` is no
stamp.

## Referencing vault notes

Several fleet commands cite the operator's Obsidian runbooks. The rule is a **wikilink by
title** (`the Manager Session runbook`) — never a filesystem path, never an `obsidian://` URL.

Vaults number their folders differently — one vault's `65 Runbooks/` is another's
`70 Runbooks/`, one's `50 Knowledge Base/` is another's `50 Knowledge/` — so any path form is
wrong in some vault by construction, while the note's *filename* is stable. A title therefore
resolves wherever the note exists.

Where a note exists in only one vault, a wikilink would dangle everywhere else, so it is
written as plain prose marked *(operator's vault; not shipped with this plugin)*. Operational
content the commands genuinely need in order to run was moved into this file instead, rather
than left behind in a single vault's runbook — a plugin should not need a particular person's
vault to describe its own mechanics.
