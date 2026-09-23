---
description: Read-only goal-or-topic snapshot — ONE goal's declared task set (from `24 Goals/`) or ONE topic's declared goal set (from `23 Topics/`), its sessions, a status table with bucket icons, and a blocked-by-you jump list (clickable jump links). No vault writes, no loop, no TTS. Subject via $1, or detected from the session when omitted. Per the [[Worker Manager Session]] runbook in the active vault.
allowed-tools:
  - Task
  - ListAgents
  - Bash(grep:*)
  - Bash(ls:*)
  - Bash(find:*)
  - Bash(awk:*)
  - Bash(python3:*)
  - Bash(mkdir:*)
  - Bash(pgrep:*)
  - Bash(ps:*)
  - Bash(vault-cli:*)
  - Bash(wezterm cli list:*)
  - Read
argument-hint: "[goal|topic] (detected when omitted)"
---

Manager-status slash command — the **read-only, one-shot** twin of `/fleet-status` for a single subject, **goal or topic**. The manager-loop loop is stateful and pings; this is a pure snapshot: "what is this tree doing right now?" Safe to run as often as you like, never sends messages, never arms the loop.

It resolves its subject **exactly as `/manager-loop` does** — same branch detection, same fallback chain, same state files — so the two commands can never disagree about which tree is being reported.

## Arguments

- **`$1` (optional — resolved from the session when omitted):** the **subject** — a goal name matching a page in `24 Goals/`, or a topic name matching a topic page in `23 Topics/`. The branch is detected from whichever page resolves; this command never assumes one.
  - `/manager-status Sentry` → topic branch, reads `23 Topics/Sentry.md`
  - `/manager-status "<Goal Name>"` → goal branch, reads `24 Goals/<Goal>.md`
  - `/manager-status` (bare) → resolve the subject from § Subject resolution below

## Subject resolution — when `$1` is omitted

**This block is shared with `/manager-loop` and `/manager-verify`. The resolution rule is identical in all three commands; the sentences that legitimately differ are enumerated here rather than counted** — a count is the part that rots: this line read *"exactly four"* for two copies, and any third consumer edits it again, which makes it a liability rather than a guarantee. **Between the two plugin copies exactly four differ** — the sibling names on this line, the STOP line, the clause ending "No fallback, ever", and the closing sentence about which contract the write changes. **`/manager-verify` differs additionally in exactly one region** — the recording paragraphs, which stay prose there because its `allowed-tools` deliberately omits `Bash(python3:*)` and `Bash(mkdir:*)`, so the runnable blocks below are not available to it. Change one, change the others; the keep-in-sync contract is the same one `/vault-cli:prepare-compact` and `/vault-cli:post-compact` carry for their check blocks. Three commands that resolve a subject differently will disagree about which tree is being reported, and the disagreement is silent.

A bare invocation takes the first source that yields a **real page**:

1. **Session state** — `~/.claude/state/worker-manager/<CLAUDE_CODE_SESSION_ID>.json`, shape `{"subject","vault","branch","resolved_at"}`. Accepted only when `vault` matches the resolved vault **and** `subject` still resolves to a page. This is what makes a subject named once stick across the ticks of one session.
2. **Session name** — the name this session carries, read from `~/.claude/sessions/$CLAUDE_PID.json` → `.name`. **`CLAUDE_PID`, not `CLAUDE_CODE_SESSION_ID`** — that directory is pid-keyed, so the session-id key the source above uses does not address it; both variables are exported, and this is the one lookup that needs the pid. Strip leading decoration before matching (`⚙ ` prefixes 11 of 47 live names), then accept only when the stripped name resolves to a goal or topic page — the same test every other source uses. A name that resolves to nothing, a name that resolves only to a **task** page, and a missing pid file are all **silent misses**: fall through to the next source, never error. Pid files are transient (the record this rule was filed from was gone hours later), and most session names are task names — measured over the 24 named Personal sessions on disk, 1 resolved to a topic, 0 to a goal, 20 to a task. **The source is narrow by construction.** It does not exist to resolve most sessions; it exists so that a session named after its own subject can never be overruled by another session's leftovers.
3. **Conversation** — the priority order `/vault-cli:task-status` uses in its Phase 2: the most recent `/manager-loop`, `/manager-status` or `/manager-verify` argument in this conversation, then the most recent goal/topic page referenced **as a subject** (a wikilink or a read/edited path — not a prose mention).
4. **Vault's last subject** — `~/.claude/state/worker-manager/last-<vault>.json`, same shape and same test. **The fallback of last resort** — below this session's own state, below its own name, below the conversation. It still earns its place: it carries a subject named in one session into another, and the vault in the filename is why two vaults never clobber each other. It ranks last because it is the only source that is not about *this* session. On 2026-09-18 a session named *Dark Factory Pipeline Hygiene* rendered a full, correct-looking snapshot of **Notification System** — this file had been written that morning by a different session, and at position 2 it outranked both the session's own name and the conversation.
5. **Nothing resolves → STOP.** Print `❌ No subject detected. Pass a goal or topic name: /manager-status "<name>"` and do nothing else.

**No fallback, ever.** Never a filename glob, a `goals:` scan, a theme match, or a content grep. The `$1` no-fallback rule exists because scope-by-globbing is the failure it prevents; a silent guess at the *subject* is the same failure one level up, and worse here — a snapshot rendered against the wrong tree reads exactly like a correct one.

**Print the source.** The first output line is `Subject: <name> (from <explicit|session|name|conversation|last>)`, so a wrong pick is interruptable before the report runs — the same reason `/vault-cli:task-status` prints `Detected task:` before its report.

**Record on explicit use.** When `$1` is supplied and resolves, write **both** files before proceeding:

```bash
mkdir -p ~/.claude/state/worker-manager && python3 -c "
import json,os,sys,datetime
subject,vault,branch=sys.argv[1:4]
d=os.path.expanduser('~/.claude/state/worker-manager'); os.makedirs(d,exist_ok=True)
rec={'subject':subject,'vault':vault,'branch':branch,'resolved_at':datetime.datetime.now().astimezone().isoformat(timespec='seconds')}
for n in (os.environ['CLAUDE_CODE_SESSION_ID']+'.json','last-'+vault+'.json'):
    json.dump(rec,open(os.path.join(d,n),'w'),indent=2)
" "$SUBJECT" "$VAULT" "$BRANCH"
```

**Record a session-local resolution too — but only the session file.** When a bare invocation resolves from the **session name** or the **conversation**, write `<CLAUDE_CODE_SESSION_ID>.json` and **never** `last-<vault>.json`, so the subject sticks across this session's later ticks without republishing this session's identity to every other session in the vault:

```bash
mkdir -p ~/.claude/state/worker-manager && python3 -c "
import json,os,sys,datetime
subject,vault,branch=sys.argv[1:4]
d=os.path.expanduser('~/.claude/state/worker-manager'); os.makedirs(d,exist_ok=True)
rec={'subject':subject,'vault':vault,'branch':branch,'resolved_at':datetime.datetime.now().astimezone().isoformat(timespec='seconds')}
json.dump(rec,open(os.path.join(d,os.environ['CLAUDE_CODE_SESSION_ID']+'.json'),'w'),indent=2)
" "$SUBJECT" "$VAULT" "$BRANCH"
```

**Never write on a `last-<vault>` resolution — neither file.** A subject taken from that file was itself only inferred, and promoting it into session state would pin it above this session's own name for every later tick. The widening is asymmetric on purpose: session-local sources may be recorded, the cross-session one may not.

**A bare invocation writes at most the session file** — `<CLAUDE_CODE_SESSION_ID>.json`, and only on a session-local resolution; never `last-<vault>`, never anything in the vault. That is why this command's contract reads "no vault writes, no messages, no loop" rather than "zero side effects": the one file it can write lives under `~/.claude/state/`, and it is keyed to this session alone.

## Resolution — detect the branch, then read the declared set (same as manager-loop)

**Identical to `/manager-loop` § Resolution step 0.** Probe with the **resolved subject**, not `$1` — under detection `$1` is empty and every probe below would match nothing. Detect the branch, never assume it:

- `find "24 Goals" -maxdepth 1 -iname "$SUBJECT.md"`, confirm `page_type: goal` in the frontmatter block → **goal branch** below.
- `find "$TOPICS_DIR" -maxdepth 1 -iname "$SUBJECT.md"`, confirm `page_type: topic` the same way → **topic branch, steps 1–5**.
- **Both match, or neither** → print every candidate path (or `no match`) and **stop**. Never guess between a goal and a topic, and never silently prefer one.

**Goal branch.** Read `24 Goals/<Goal>.md`.

- **Tracked set** = every task whose `goals:` frontmatter names this goal — exact match on the **goal name**, across **all three `goals:` shapes** (list of `[[wikilinks]]`, plain scalar `goals: X`, nested `- - X`); the census and the failure mode live in `/manager-loop` § Resolution — read them there, do not re-derive them here. A wikilink-only match is non-compliant: it renders the tracked set **smaller and complete-looking**, with no error, while every probe passes. This is the declaration; there is no second source.
- **Cross-check against the goal's `# Tasks` list.** A task listed there whose `goals:` does not name the goal, or one whose `goals:` names it but which is absent from `# Tasks`, is a **finding to report** — never silently unioned.
- Print a header line `Goal: <status> · <n>/<m> SC` read from the page, then the same table and the same blocked-by-you jump list the topic branch renders. The `Tracked (N):` line lists the goal's tasks.
- **Closure is not yours.** Report SC status and hand closure to `/vault-cli:complete-goal` — never tick SC or flip the goal's status.
- There is no `# Goals` list, no declared-optional set and no `# Status Summary` on a goal. Do not synthesise them; **skip steps 1–5** and go straight to the Procedure.

**Topic branch** — steps 1–5 below.

1. **Resolve the topics folder from config, then find the topic page** — the folder is declared per vault, not hardcoded: `TOPICS_DIR=$(vault-cli config list --output json 2>/dev/null | python3 -c "import json,sys,os;cwd=os.path.realpath(os.getcwd());d=json.load(sys.stdin);print(next((v.get('topics_dir') or '23 Topics' for v in d if os.path.realpath(os.path.expanduser(v['path']))==cwd),'23 Topics'))" 2>/dev/null || echo "23 Topics")`, then `find "$TOPICS_DIR" -maxdepth 1 -iname "$SUBJECT.md"`, confirm `page_type: topic` **in the frontmatter block** (`awk '/^---$/{n++; next} n==1' <page> | grep -q '^page_type: topic'` — unscoped would match the guide's YAML template). Never accept `Topic Writing Guide.md` (it lives in the vault's knowledge-base folder, outside this folder). **`-iname`, not a shell glob** — a plain `ls` glob is case-sensitive under zsh, so a lowercase argument resolves nothing against Title Case filenames (verified 2026-09-12: `*"discord"*.md` → `no matches found`, `*"Discord"*.md` → 5 files), and step 5's no-fallback rule then reports an existing page as missing.
2. **Read its `## Goals` list** — **An entry in a topic's `## Goals` list may be a goal or a task: a goal admits every task whose `goals:` names it, and a task entry admits that task directly — membership is read from the page and never re-derived.** Tracked set = every declared goal + every task whose `goals:` frontmatter names one of them, across **all three `goals:` shapes**, + every task named directly as an entry (see `/manager-loop` § Resolution for the census and the naive-`\[\[…\]\]` failure mode). **The heading is `## Goals` (a sub-heading under `# Scope`), not `# Goals`** — a literal `^# Goals` match returns nothing, and with no fallback that reads as an empty tracked set rather than as a miss.
3. **Resolve the declared-optional set** — the subset of tracked **task** names the page declares optional. Same rule as `/manager-loop` Resolution step 3 and [[Worker Manager Session]] § Step 4: match **semantically** (wikilink-suffix *and* Non-goals/backticked shapes both occur — keying on the literal `optional (operator, <date>)` misses the second), **never infer from `status`** (`hold`/`backlog` are orthogonal dispositions; the resemblance on Notification System is a coincidence of one page). ⚠️ **Read it at whichever level the page declares it — a goal OR a task.** A goal declared optional contributes **all** of its tasks to the set (the Mantra lane is declared optional as a goal, so all five of its tasks are in it even though no line names three of them), and a task declared optional on its own is in it too. **The set you pass on is always task names** — the frame places tasks, and a required goal owning an optional task renders as a goal row in both boxes, which is correct rather than a defect to collapse. Nothing declared → empty set → one unlabelled box.
4. **Read its `# Status Summary`** — hand-written prose, context only; the snapshot still reads task files directly.
5. **No topic page** — print the missing page name and the fix (`create 23 Topics/<Topic>.md from the [[Topic Writing Guide]] template`), then stop. **Never** fall back to a filename glob, `goals:` scan, theme match, or content grep — a silent fallback is what let scope be re-derived by globbing.

Print the resolution header first, then the tracked set once:

```text
Subject: [<name>](obsidian://open?vault=<V>&file=<relpath>) (from <explicit|session|last|conversation>)
Branch: <goal|topic> (<the page it came from>)
Goal: <status> · <n>/<m> SC          ← goal branch only
Tracked (N): <task> · <task> …
```

**The subject is always a clickable link.** The operator should be able to open the page the snapshot describes without hunting for it, so the link is part of the header and not an optional extra. Build it per the vault link convention — `obsidian://open?vault=<V>&file=<relpath>`, percent-encoding every character outside `[A-Za-z0-9-_.~]` (space → `%20`, `/` → `%2F`, `→` → `%E2%86%92`) and **dropping the `.md`**. `<V>` is the vault name and `<relpath>` the page path minus the vault root, both read from `vault-cli config list --output json` — never hand-written, because a hand-written path is how a link that looks right opens the wrong page.

The `Subject:` line leads so a mis-resolution is visible before the table renders, and `Branch:` names the page it came from so a wrong branch is legible immediately — the same reason `/manager-loop` prints its branch in the header. Neither line is a verdict; both are facts read from disk.

## Procedure

1. `ListAgents` — the subject's worker sessions (match by task name), status, age.
2. **Delegate the computation to `supervisor:manager-sweep-reader`** — invoke it as `Task(subagent_type: "supervisor:manager-sweep-reader", prompt: <tracked set + declared-optional set + this snapshot's roster verbatim + vault + mode: snapshot>)`. The agent ships here, in `agents/manager-sweep-reader.md` — not in `~/.claude/agents/`; the plugin prefix is required, since a bare name resolves to a personal copy and never to a plugin agent. Pass it the tracked set, **the declared-optional set** (from Resolution step 3 — pass it every run, even when empty; omitting it here while `/manager-loop` passes it is exactly how the two commands diverge, which is the failure this shared agent exists to prevent), this snapshot's `ListAgents` roster **verbatim** (a subagent has no address of its own and cannot call `ListAgents` — the roster is an input), the vault, and `mode: snapshot`. **Pass no timestamp** — a snapshot is not a tick and carries no marker; the agent reads no clock of its own. It owns the tracked-set read, the bucket classification, the unanchored id-set extraction, the collision count, and the table render, and returns one compact report: table · counts · orphan candidates · live collisions · delta.
3. **When the delegation returns no usable table — render it yourself with `box-table.py`.** The agent is the only thing that computes the classification, but it is not the only thing that can draw the box, and the box is what the operator reads. **Trigger on the delegation returning no usable table** — it errored, came back empty, or resolved to something that did not return the report. **Never on a matched error string**: the failure has been observed as a harness message (`The following agent types are no longer available: manager-sweep-reader`), as a silent non-return, and as a bare-name dispatch that succeeded with a *correct* table — so no single message identifies it, and a trigger keyed to one would miss the other two. Same trigger, same wording, in `/manager-loop`; the two commands' fallback text is shared, like the rest of their keep-in-sync block.
   Build the row JSON from the inputs you already hold — the tracked set, the roster, and the declared-optional set are all passed to the agent and are all in your hands — then render with `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/box-table.py`, reading its stdin contract at [[Worker Manager Session]] § Sweep output. **The frame must still match a normal snapshot's** — same columns, same widths, same icons, same indent — because § Sweep output stays the single source for the frame and this fallback points at it rather than restating it. **Still never hand-draw the box.** The fallback is a second *renderer*, not a licence to draw one by hand; every misaligned table this runbook has carried was hand-drawn, which is the failure the rule exists for.
   **Say so in the snapshot when it fires.** A snapshot that rendered through the fallback is one whose delegation failed — print that in the output rather than letting a correct-looking table imply the agent ran. **What this does not cover:** a delegation that resolves and returns a *wrong-but-plausible* table. That produces no signal to trigger on, so the fallback cannot catch it; it is out of scope here and would need a different guard.
4. Print the table — from the agent, or from the fallback above. It renders per [[Worker Manager Session]] § Sweep output — the status table, which stays the single source for the frame, the columns, the widths and the icons; this command must never restate them and never hand-draw the box. `/manager-loop` delegates to the same agent, so both commands render identically by construction rather than by two files agreeing to. Below the box, add only the non-empty action lines defined in § Sweep output — this command does not restate them. **The agent returns orphan *candidates*; print an `ORPHANED` line only after confirming it** — the confirmation probe and the reason it cannot live in the agent are in [[Worker Manager Session]] § Step 4. Plan-gate parking is the agent's classification; its rule is in that same section — do not restate it here.
5. **Blocked-by-you jump list** (same shape as `/fleet-status`): the subject's `waiting` sessions, resolved to **pane ids** via `wezterm cli list` (TITLE == session name), one jump per row + "Next blocker to jump to" (oldest wait first):

   ```text
   ⌛ Blocked by you (N waiting — waiting ≠ confirmed blocked; verify before acting)
     1. <name> — <age> · jump: <jump-link.py PANEID>
     Next blocker to jump to: <name> → <jump-link.py PANEID --label>
   ```

   **Each `jump:` field is the one-line output of `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/jump-link.py <PANEID>` — never a hand-written URL, and never the token.** With the fleet-jump server configured that is a clickable `http://127.0.0.1:1337/jump?pane=<N>&t=…` link, followed with **SHIFT+CMD+click**; without it the script prints the `/supervisor:jump <N>` command, so the row is always usable. **Never print a bare URL you built yourself** — the token lives in a 0600 file outside every repo, so a hand-written link is either broken or leaks it into the repo. Printing a URL mutates nothing, which is what keeps this command inside its no-mutation contract.

   **Emit the link — never a raw `wezterm cli activate-tab` line, and never a tab id.** The reasons are measured, not stylistic. `activate-tab` **cannot cross WezTerm windows**: called from another window it succeeds and nothing visibly moves, so the operator gets a silent no-op instead of a jump. A tab id is **renumbered when its tab moves windows**, so a handed-over `--tab-id` goes dead within the hour — 2026-09-18, three spawned workers routed as tabs 158/159/160 in window 0 became tabs 163/164/165 in window 2, and `activate-tab --tab-id 159` failed outright with *"could not determine which pane should be active"* while `activate-pane --pane-id 239` worked immediately. And a **full WezTerm restart renumbers both namespaces at once**, collapsing them to small integers — the same day, every worker's pane id changed (238 → 33, 241 → 31, 257 → 36, 239 → 39, 255 → 44). `/supervisor:jump` resolves the coordinate at run time and prints the window id, so a cross-window no-op is legible rather than confusing. **Hand over a pane id** — the more stable of the two handles — and re-resolve it after any restart; never trust a coordinate recorded earlier in the session. ⚠️ **A bare `/supervisor:jump <N>` can refuse when tab N and pane N are both live and disagree** — the ambiguity rule. That is correct behaviour, not a failure: re-emit as `/supervisor:jump pane:<N>` and it resolves.
6. **Names lead, numbers serve the command.** Every row and every mention leads with the task/session name; the `[ref]` and pane id are secondary, for the command only — never reference a session by bare number in prose.
7. **Never fabricate state** — report only what `ListAgents`/task files show this sweep. No recommendation, no verdict, no `👤 You:`/`⏰ Next:` panel, no TTS, no `SendMessage`. For "what should I do about this goal or topic," that is `/manager-loop`'s loop, not this snapshot.
