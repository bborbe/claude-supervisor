# Subject resolution

The rule four commands share: `/supervisor:manager-loop`, `/supervisor:manager-status`, `/supervisor:manager-drive` and `/supervisor:manager-verify`. Each carries **a pointer to this file**, its own STOP line, and — where it can — its own recording form. Nothing else of the rule is restated there.

**Why this is one file now.** The block was copied into all four, with a keep-in-sync sentence in each naming the others. Three commands that resolve a subject differently will disagree about which tree is being reported, and the disagreement is silent — so the copy was the liability, not the guarantee. Written out 2026-09-25, when `manager-verify` was extracted into a command+agent pair and the fourth copy would otherwise have been rewritten for the third time.

## Resolve the vault first

**From the session's cwd, never a default.** A session in `~/Documents/Obsidian/Brogrammers` named *MDM Bugs* must resolve `Brogrammers/23 Topics/MDM Bugs.md`, not the Personal vault's.

```bash
VAULT=$(vault-cli config list --output json | python3 -c "import json,sys,os;cwd=os.path.realpath(os.getcwd());print(next((v['name'] for v in json.load(sys.stdin) if os.path.realpath(os.path.expanduser(v['path']))==cwd),''))")
```

Every page test in § The page test (`24 Goals/`, the vault's `topics_dir`) runs inside that vault. No match → STOP with `❌ cwd is not a configured vault — pass a goal or topic name and run from the vault root`; **never fall back to `personal`**.

⚠️ **Who runs this paragraph, and who cannot.** `/manager-loop` and `/manager-status` derive the vault from their own `## Resolution` section and skip it. `/manager-drive` has no such section, so it carries the paragraph and runs it — it holds `Bash(python3:*)` and `Bash(vault-cli:*)`. ⚠️ **`/manager-verify` is the third case: it has no `## Resolution` section either, and its 2026-09-20 read-only re-scope trimmed `Bash(python3:*)`, `Bash(mkdir:*)` and `Bash(vault-cli:*)`** — so it can run neither this lookup nor the recording block below. It resolves the vault from cwd through its own literal `24 Goals/` / `23 Topics/` probes, which already assume the vault root, and records nothing. **That is not a regression** — it could never run either block; the extraction only made the gap visible. Its body states both divergences.

## The page test

**Exact basename inside the folder. The folder is the discriminator, not `page_type`.**

```bash
find "24 Goals" -maxdepth 1 -iname "$SUBJECT.md"       # goal
find "$TOPICS_DIR" -maxdepth 1 -iname "$SUBJECT.md"    # topic
```

**`-iname`, never a case-sensitive shell glob.** A plain `ls "23 Topics/"*"$SUBJECT"*.md` is case-sensitive under zsh, so a lowercase argument silently resolves nothing against Title Case filenames (verified 2026-09-12: `*"discord"*.md` → `no matches found`, `*"Discord"*.md` → 5 files). The no-fallback rule then reports an existing page as missing.

**Never a substring glob** (`*$SUBJECT*.md`). A topic's member goals are usually named after it, so a wildcard makes one argument match in both folders — `/manager-loop Sentry` collided with `24 Goals/Sentry Bug Batch 1.md` and `24 Goals/Autonomous Sentry Triage Agent for Octopus.md` (observed 2026-09-17). Under exact matching, two matches inside one folder are impossible by construction.

**`page_type` is required on the topic side only — the asymmetry is measured, not stylistic.** The field was required on both sides to reject the convention guides that describe these page kinds: `Topic Writing Guide.md` carries no `page_type` and shows one inside its YAML template block, so an unscoped grep resolves the guide that *describes* topics as if it were one. Those guides live in the vault's knowledge folder, **outside** both folders, so the scoped `find` above already excludes them. Measured 2026-09-27 in Personal: **88 of 258** goal pages carried no `page_type: goal` and were therefore unresolvable to the pre-dispatch gate *and* to the commands that defer to it — a third of the goal branch unreachable, for a field that bought no discrimination — while **11 of 11** topic pages carried `page_type: topic`. So the goal branch keys on `24 Goals/<name>.md` existing, and only the topic branch additionally requires

```bash
awk '/^---$/{n++; next} n==1' "<page>" | grep -q '^page_type: topic'
```

**Both folders match, or neither → STOP.** Print every candidate path (or `no match`) and stop. Never guess between a goal and a topic, and never silently prefer one. A name that matches neither is not tracked — that is the intended pressure.

## The four sources

A bare invocation takes the **first source that yields a real page**:

1. **Session state** — `~/.claude/state/worker-manager/<CLAUDE_CODE_SESSION_ID>.json`, shape `{"subject","vault","branch","resolved_at"}`. Accepted only when `vault` matches the resolved vault **and** `subject` still resolves to a page. Compare **case-insensitively** — the vault is the lowercase vault-cli config `name` (`personal`), but state files written before 2026-09-23 may carry display case (`Personal`); a strict match silently drops to source 2. This is what makes a subject named once stick across the ticks of one session.

2. **Session name** — the name this session carries, read from `~/.claude/sessions/$CLAUDE_PID.json` → `.name`. **`CLAUDE_PID`, not `CLAUDE_CODE_SESSION_ID`** — that directory is pid-keyed, so the session-id key source 1 uses does not address it; both variables are exported, and this is the one lookup that needs the pid. Strip leading decoration before matching (`⚙ ` prefixes 11 of 47 live names), then accept only when the stripped name resolves to a goal or topic page — the same test every other source uses. A name that resolves to nothing, a name that resolves only to a **task** page, and a missing pid file are all **silent misses**: fall through to the next source, never error. Pid files are transient (the record this rule was filed from was gone hours later), and most session names are task names — measured over the 24 named Personal sessions on disk, 1 resolved to a topic, 0 to a goal, 20 to a task. **The source is narrow by construction.** It does not exist to resolve most sessions; it exists so that a session named after its own subject can never be overruled by another session's leftovers.

3. **Conversation** — the priority order `/vault-cli:task-status` uses in its Phase 2: the most recent `/supervisor:manager-loop`, `/supervisor:manager-status`, `/supervisor:manager-drive` or `/supervisor:manager-verify` argument in this conversation, then the most recent goal/topic page referenced **as a subject** (a wikilink or a read/edited path — not a prose mention). ⚠️ **This source cannot move into an agent.** A subagent runs in a fresh context and cannot see the session transcript, so every command keeps this one source inline; it is the reason the rule is a doc with four inline pointers rather than a single shared call.

4. **Vault's last subject** — `~/.claude/state/worker-manager/last-<vault>.json`, same shape and same test. **The fallback of last resort** — below this session's own state, below its own name, below the conversation. It still earns its place: it carries a subject named in one session into another, and the vault in the filename is why two vaults never clobber each other. It ranks last because it is the only source that is not about *this* session. On 2026-09-18 a session named *Dark Factory Pipeline Hygiene* rendered a full, correct-looking snapshot of **Notification System** — this file had been written that morning by a different session, and at position 2 it outranked both the session's own name and the conversation.

5. **Nothing resolves → STOP.** Print `❌ No subject detected. Pass a goal or topic name: /<the command> "<name>"` and do nothing else.

## No fallback, ever

Never a filename glob, a `goals:` scan, a theme match, or a content grep. A silent guess at the subject is the failure this whole chain exists to prevent.

## Print the source

The first output line is `Subject: <name> (from <explicit|session|name|conversation|last>)`, so a wrong pick is interruptable before the report runs — the same reason `/vault-cli:task-status` prints `Detected task:` before its report.

⚠️ **Why the source and the branch are printed — and what this guard no longer claims.** The guard used to read *"**This command mutates**: steps 2, 4, 6 and 7 prune, author, plan and spawn … A wrong subject here does not misreport, it **acts on the wrong tree**"*. **That is no longer true.** The 2026-09-20 re-scope made `manager-verify` read-only and turned those steps into **suggestions**. A wrong subject now **misreports**, exactly as in the read-only siblings — so the guard is the same one they carry, and it is **`print the source`**. Kept as a correction rather than deleted: the retracted claim is the one a reader would otherwise re-derive from the block's absence.

The branch line follows it, exactly as in the siblings: `Branch: <goal|topic> (<the page it came from>)`.

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

⚠️ **`/manager-verify` cannot run the block above, and the divergence covers two halves, not one.** Its `allowed-tools` omits `Bash(python3:*)`, `Bash(mkdir:*)` **and `Bash(vault-cli:*)`**, so it can run neither the vault lookup nor the recording write. It follows the **contract** (which files, which cases) rather than the snippet, and states both divergences in its own body. Intentional, and the only region where its copy legitimately diverges.
