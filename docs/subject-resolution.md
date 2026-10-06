# Subject resolution

The rule four commands share: `/supervisor:manager-loop`, `/supervisor:manager-status`, `/supervisor:manager-drive` and `/supervisor:manager-verify`. Each carries **a pointer to this file**, its own STOP line, and — where it can — its own recording form and its own subject-status reconcile. Nothing else of the rule is restated there. ⚠️ **"Where it can" is doing real work in that sentence, and § *Reconcile the subject's status* is where both carve-outs are named:** `/manager-status` is withheld by its own **no-vault-write contract** rather than by a missing capability, and `/manager-verify` by a **missing permission grant**.

**Why this is one file now.** The block was copied into all four, with a keep-in-sync sentence in each naming the others. Three commands that resolve a subject differently will disagree about which tree is being reported, and the disagreement is silent — so the copy was the liability, not the guarantee. Written out 2026-09-25, when `manager-verify` was extracted into a command+agent pair and the fourth copy would otherwise have been rewritten for the third time.

## Resolve the vault first

**From the session's cwd, never a default.** A session in `~/Documents/Obsidian/other-vault` named *Sample Task* must resolve `other-vault/23 Topics/Sample Task.md`, not the primary vault's.

```bash
VAULT=$(vault-cli config list --output json | python3 -c "import json,sys,os;cwd=os.path.realpath(os.getcwd());print(next((v['name'] for v in json.load(sys.stdin) if os.path.realpath(os.path.expanduser(v['path']))==cwd),''))")
```

Every page test in § The page test (`24 Goals/`, the vault's `topics_dir`) runs inside that vault. No match → STOP with `❌ cwd is not a configured vault — pass a goal or topic name and run from the vault root`; **never fall back to a default vault**.

⚠️ **Who runs this paragraph, and who cannot.** `/manager-loop` and `/manager-status` derive the vault from their own `## Resolution` section and skip it. `/manager-drive` has no such section, so it carries the paragraph and runs it — it holds `Bash(python3:*)` and `Bash(vault-cli:*)`. ⚠️ **`/manager-verify` is the third case: it has no `## Resolution` section either, and its 2026-09-20 read-only re-scope trimmed `Bash(python3:*)`, `Bash(mkdir:*)` and `Bash(vault-cli:*)`** — so it can run neither this lookup nor the recording block below. It resolves the vault from cwd through its own literal `24 Goals/` / `23 Topics/` probes, which already assume the vault root, and records nothing. **That is not a regression** — it could never run either block; the extraction only made the gap visible. Its body states all three divergences.

## The page test

**Exact basename inside the folder. The folder is the discriminator, not `page_type`.**

```bash
find "24 Goals" -maxdepth 1 -iname "$SUBJECT.md"       # goal
find "$TOPICS_DIR" -maxdepth 1 -iname "$SUBJECT.md"    # topic
```

**`-iname`, never a case-sensitive shell glob.** A plain `ls "23 Topics/"*"$SUBJECT"*.md` is case-sensitive under zsh, so a lowercase argument silently resolves nothing against Title Case filenames (verified 2026-09-12: `*"discord"*.md` → `no matches found`, `*"Discord"*.md` → 5 files). The no-fallback rule then reports an existing page as missing.

**Never a substring glob** (`*$SUBJECT*.md`). A topic's member goals are usually named after it, so a wildcard makes one argument match in both folders — `/manager-loop Sentry` collided with `24 Goals/Sentry Bug Batch 1.md` and `24 Goals/Autonomous Sentry Triage Agent for Octopus.md` (observed 2026-09-17). Under exact matching, two matches inside one folder are impossible by construction.

**`page_type` is required on the topic side only — the asymmetry is measured, not stylistic.** The field was required on both sides to reject the convention guides that describe these page kinds: `Topic Writing Guide.md` carries no `page_type` and shows one inside its YAML template block, so an unscoped grep resolves the guide that *describes* topics as if it were one. Those guides live in the vault's knowledge folder, **outside** both folders, so the scoped `find` above already excludes them. Measured 2026-09-27 in the primary vault: **88 of 258** goal pages carried no `page_type: goal` and were therefore unresolvable to the pre-dispatch gate *and* to the commands that defer to it — a third of the goal branch unreachable, for a field that bought no discrimination — while **11 of 11** topic pages carried `page_type: topic`. So the goal branch keys on `24 Goals/<name>.md` existing, and only the topic branch additionally requires

```bash
awk '/^---$/{n++; next} n==1' "<page>" | grep -q '^page_type: topic'
```

**Both folders match, or neither → STOP.** Print every candidate path (or `no match`) and stop. Never guess between a goal and a topic, and never silently prefer one. A name that matches neither is not tracked — that is the intended pressure.

## The four sources

A bare invocation takes the **first source that yields a real page**:

1. **Session state** — `~/.claude/state/worker-manager/<CLAUDE_CODE_SESSION_ID>.json`, shape `{"subject","vault","branch","resolved_at"}`. Accepted only when `vault` matches the resolved vault **and** `subject` still resolves to a page. Compare **case-insensitively** — the vault is the lowercase vault-cli config `name` (`private-personal`), but state files written before 2026-09-23 may carry display case (`MyVault`); a strict match silently drops to source 2. This is what makes a subject named once stick across the ticks of one session.

2. **Session name** — the name this session carries, read from `~/.claude/sessions/$CLAUDE_PID.json` → `.name`. **`CLAUDE_PID`, not `CLAUDE_CODE_SESSION_ID`** — that directory is pid-keyed, so the session-id key source 1 uses does not address it; both variables are exported, and this is the one lookup that needs the pid. Strip leading decoration before matching (`⚙ ` prefixes 11 of 47 live names), then accept only when the stripped name resolves to a goal or topic page — the same test every other source uses. A name that resolves to nothing, a name that resolves only to a **task** page, and a missing pid file are all **silent misses**: fall through to the next source, never error. Pid files are transient (the record this rule was filed from was gone hours later), and most session names are task names — measured over the 24 named primary-vault sessions on disk, 1 resolved to a topic, 0 to a goal, 20 to a task. **The source is narrow by construction.** It does not exist to resolve most sessions; it exists so that a session named after its own subject can never be overruled by another session's leftovers.

3. **Conversation** — the priority order `/vault-cli:task-status` uses in its Phase 2: the most recent `/supervisor:manager-loop`, `/supervisor:manager-status`, `/supervisor:manager-drive` or `/supervisor:manager-verify` argument in this conversation, then the most recent goal/topic page referenced **as a subject** (a wikilink or a read/edited path — not a prose mention). ⚠️ **This source cannot move into an agent.** A subagent runs in a fresh context and cannot see the session transcript, so every command keeps this one source inline; it is the reason the rule is a doc with four inline pointers rather than a single shared call.

4. **Vault's last subject** — `~/.claude/state/worker-manager/last-<vault>.json`, same shape and same test. **The fallback of last resort** — below this session's own state, below its own name, below the conversation. It still earns its place: it carries a subject named in one session into another, and the vault in the filename is why two vaults never clobber each other. It ranks last because it is the only source that is not about *this* session. On 2026-09-18 a session named *a dark-factory guide* rendered a full, correct-looking snapshot of **Notification System** — this file had been written that morning by a different session, and at position 2 it outranked both the session's own name and the conversation.

5. **Nothing resolves → STOP.** Print `❌ No subject detected. Pass a goal or topic name: /<the command> "<name>"` and do nothing else.

## No fallback, ever

Never a filename glob, a `goals:` scan, a theme match, or a content grep. A silent guess at the subject is the failure this whole chain exists to prevent.

## Print the source

The first output line is `Subject: <name> (from <explicit|session|name|conversation|last>)`, so a wrong pick is interruptable before the report runs — the same reason `/vault-cli:task-status` prints `Detected task:` before its report.

⚠️ **Why the source and the branch are printed — and what this guard no longer claims.** The guard used to read *"**This command mutates**: steps 2, 4, 6 and 7 prune, author, plan and spawn … A wrong subject here does not misreport, it **acts on the wrong tree**"*. **That is no longer true.** The 2026-09-20 re-scope made `manager-verify` read-only and turned those steps into **suggestions**. A wrong subject now **misreports**, exactly as in the read-only siblings — so the guard is the same one they carry, and it is **`print the source`**. Kept as a correction rather than deleted: the retracted claim is the one a reader would otherwise re-derive from the block's absence.

The branch line follows it, exactly as in the siblings: `Branch: <goal|topic> (<the page it came from>)`.

## Reconcile the subject's status

**Resolution is once per session, so this is a resolution step — never a per-sweep one.** Having resolved a subject **and printed its `Branch:` line**, read its page's frontmatter `status` and, **only when** it reads `todo`/`next` **or is absent**, set it to `in_progress`. ⚠️ **It runs once the branch is known, not merely "before the sweep"** — both inputs it needs (the page path and the detected branch) are produced by § The page test, so a literal reading that runs it earlier leaves it guessing the branch, which is the case that silently takes the goal write. ⚠️ **The condition is the guard, not decoration** — a write that skips it lands `in_progress` over a `hold`.

⚠️ **This section carries no runnable snippet, deliberately.** It carried one through six review rounds and it was wrong a different way each time: inputs never assigned anywhere in the tree, an `awk` that never matched a CRLF delimiter, an external `tr`/`sed` that neither writer's `allowed-tools` grants, a branch test whose `else` silently made the goal write the default, and a read keyed on the resolved page while the write was keyed on the raw name. **A markdown block is not executed by `make test`**, so every one of those was a claim nothing could check — and it read as authoritative the whole time, which is the failure mode, not a footnote to it. The guarantees are stated instead, and each is a thing to satisfy rather than a line to copy:

- **Read the page § The page test found** — the path it returned, never a re-derivation from the subject name. That test matches with `-iname`, so a plain `"$TOPICS_DIR/$SUBJECT.md"` join misses a subject typed in a different case than its filename.
- **Refuse on an unreadable page, before the read.** ⚠️ **This one is load-bearing.** Every path that leaves the status empty for a reason *other* than a genuinely absent field takes the same write arm as an absent field, so a wrong page must fail closed rather than be read as *no status*.
- **Normalise before you parse, then normalise the value — they are two steps, and the first is the one that matters.** ⚠️ **The CR has to come off *every line* before the frontmatter delimiter is matched**: `/^---$/` never matches `---\r`, so on a CRLF page the counter never advances, no `status:` line is read at all, and the empty result is indistinguishable from a genuinely absent field. Stripping a trailing CR from the *value* does not fix that — the value is never reached. Then strip trailing whitespace and fold case on what the read returned. This doc mandates case-insensitive comparison three times; the guard is no exception.
- **Branch on the branch § The page test detected**, and **write through that branch's own command** — `vault-cli goal set "<subject>" status in_progress --vault "$VAULT"` on the goal branch, `vault-cli topic set "<subject>" status in_progress --vault "$VAULT"` on the topic. ⚠️ **The shape is `set <name> <key> <value>` with `--vault` passed explicitly** — verified against `vault-cli goal set --help`, and matching what the tree's other `vault-cli` writes do. ⚠️ **No other verb writes a goal's status:** every closure-shaped status change goes through `/vault-cli:complete-goal`, and a bare `vault-cli task set` addresses tasks, not the subject page. ⚠️ **Never let one branch be the fallback** — a branch that is unset or mistyped and silently takes the goal write puts a topic's name into a goal command.
- **Write the name derived from the page you read**, not the raw subject argument, or the read and the write can address different pages.
- **Use only binaries both writers are granted.** `awk` and `vault-cli` are; `tr` and `sed` are not, and a pipeline that reaches for them parks on an approval the command was never scoped for.

⚠️ **There are three reads, and their order is part of the contract: guard → pre-write re-read → post-write read-back.**

⚠️ **The read and the write are two acts, so an operator flip landing between them is lost.** The window is narrow — this fires once, at resolution — but the status at risk is `hold`, which this section calls irreplaceable, and because the reconcile is **once per session** the loss is not self-correcting for the life of the session. ⚠️ **So the pre-write re-read is mandatory, not advisory:** read the status again immediately before the write, and say which read the write was based on. **A value that moved between the two reads is a refusal, not a write** — that is the one path by which this section's own load-bearing guard can be defeated, and it costs one command to close.

⚠️ **Then read the page back and say what it now reads** — the third and last read, taken *after* the write. This is the one write in the file that had no read-back contract, and a silent failure leaves the defect above standing for the whole life of the session. Present → `✅ Reconciled: <subject> <old> → in_progress`. Absent, or unchanged → `⚠️ Not reconciled — <subject> still reads <value>`, and continue; **never proceed as though the write landed.**

⚠️ **Why, measured 2026-10-06.** A manager tick resolved the goal `Attention Controller Ultra-Fast Reads`, swept its declared set and printed a table every tick — while the goal page itself read `status: todo` (the legacy alias for `next`) and a task under it sat at `phase: execution` with a live worker. Every reader — the operator, the sweep table, the model-free gate — saw queued work where there was running work, and the operator flipped the page by hand. **The manager is the one party that already knows the difference**, because it is the party that resolved the subject; leaving the page at `todo` makes its own report read as a tree it has not started.

⚠️ **Once at resolution, never per wakeup.** This rides the rule the subject-and-branch *detection* already carries: resolution happens once at manager start, so the reconcile fires once. A per-tick reconcile would fight the operator — a page deliberately set back to `next` would be re-flipped on the next firing, with nothing saying so.

⚠️ **It writes `in_progress` over `todo`/`next`/absent, and nothing else.** An already-`in_progress` subject is left alone (the ordinary case), and so are `hold`, `backlog` and the terminal `completed`/`aborted` — a status someone chose deliberately is not this clause's to overrule. ⚠️ **`hold` especially:** it marks a block with no scheduled resume date, so overwriting it would erase the only record that the subject is parked.

⚠️ **`# Success Criteria` are untouched, and this is not the closure path.** The clause flips `status` only. The subject's `# Success Criteria` stay its closure contract and still close **mechanically** through `/vault-cli:complete-goal`. ⚠️ **This is the sentence `/manager-loop` § Resolution step G's *"never tick SC yourself, and never flip the goal's status in order to close it"* must be read against** — that rule governs **closure**, and a resolution-time `status` write is not closure. Where the two are read as one rule, the closure half is what is lost: a manager that cannot set `in_progress` leaves the defect above standing.

⚠️ **Who runs this — and it is not all four resolvers.** The write needs `Bash(vault-cli:*)`; **`manager-verify` does not hold that grant, and `manager-status` holds it but is barred by its own read-only contract** — two different reasons, and only one of them a missing capability:

| Command | Resolves a subject | Reconciles `status` |
|---|---|---|
| `/manager-loop` | yes | **yes** |
| `/manager-drive` | yes | **yes** |
| `/manager-status` | yes | **no** — read-only by its own contract (*"No vault writes"*) |
| `/manager-verify` | yes | **no** — holds no `Bash(vault-cli:*)` |

⚠️ **This is a write-scope carve-out, never a resolution one** — both non-writers still resolve, and neither is a *"does not resolve a subject"* case to fall back on. They report the page's status as they find it, exactly as before. `manager-status` states the read-only contract in its own frontmatter, and `docs/fleet-surface.md` § *Session roles* calls the status/drive split *"the look from the act"*; `manager-verify`'s trim is the same one § Resolve the vault first and § Recording already record. Each non-writer's body states its own divergence.

## Recording

**Record on explicit use.** With an argument supplied and resolving, write **both** `~/.claude/state/worker-manager/<session-id>.json` and `last-<vault>.json`:

```bash
mkdir -p ~/.claude/state/worker-manager && python3 -c "
import json,os,sys,datetime
subject,vault,branch=sys.argv[1:4]; vault=vault.lower()
d=os.path.expanduser('~/.claude/state/worker-manager'); os.makedirs(d,exist_ok=True)
rec={'subject':subject,'vault':vault,'branch':branch,'resolved_at':datetime.datetime.now().astimezone().isoformat(timespec='seconds')}
for n in (os.environ['CLAUDE_CODE_SESSION_ID']+'.json','last-'+vault+'.json'):
    json.dump(rec,open(os.path.join(d,n),'w'),indent=2)
" "$SUBJECT" "$VAULT" "$BRANCH"
```

**On a session-name or conversation resolution, write the session file only** — never `last-<vault>`. Promoting an inferred subject into cross-session state pins it above this session's own name for every later tick.

**On a `last-<vault>` resolution, write neither.** A subject taken from that file was itself only inferred, and promoting it would widen the same way. The asymmetry is deliberate: session-local sources may be recorded, the cross-session one may not.

⚠️ **`/manager-verify` cannot run the block above — nor either of this file's other two executable blocks — and the divergence covers three, not two.** Its `allowed-tools` omits `Bash(python3:*)`, `Bash(mkdir:*)` **and `Bash(vault-cli:*)`**, so it can run none of: the vault lookup in § *Resolve the vault first*, the recording write above, and the reconcile in § *Reconcile the subject's status*. It follows the **contract** (which files, which cases) rather than the snippets, and states all three divergences in its own body. Intentional, and the only region where its copy legitimately diverges.
