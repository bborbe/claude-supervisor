# Subject resolution

The rule four commands share: `/supervisor:manager-loop`, `/supervisor:manager-status`, `/supervisor:manager-drive` and `/supervisor:manager-verify`. Each carries **a pointer to this file**, its own STOP line, and — where it can — its own recording form. Nothing else of the rule is restated there.

**Why this is one file now.** The block was copied into all four, with a keep-in-sync sentence in each naming the others. Three commands that resolve a subject differently will disagree about which tree is being reported, and the disagreement is silent — so the copy was the liability, not the guarantee. Written out 2026-09-25, when `manager-verify` was extracted into a command+agent pair and the fourth copy would otherwise have been rewritten for the third time.

## Resolve the vault first

**From the session's cwd, never a default.** A session in `~/Documents/Obsidian/Brogrammers` named *MDM Bugs* must resolve `Brogrammers/23 Topics/MDM Bugs.md`, not the Personal vault's.

```bash
VAULT=$(vault-cli config list --output json | python3 -c "import json,sys,os;cwd=os.path.realpath(os.getcwd());print(next((v['name'] for v in json.load(sys.stdin) if os.path.realpath(os.path.expanduser(v['path']))==cwd),''))")
```

Every page test below (`24 Goals/`, the vault's `topics_dir`) runs inside that vault. No match → STOP with `❌ cwd is not a configured vault — pass a goal or topic name and run from the vault root`; **never fall back to `personal`**.

⚠️ Commands that carry their own `## Resolution` section derive the vault from it instead and skip this paragraph; `manager-drive` carries it because it has no such section.

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

⚠️ **A command whose `allowed-tools` omits `Bash(python3:*)` / `Bash(mkdir:*)` cannot run the block above** — `manager-verify` is the one such copy, because its read-only re-scope trimmed its tool surface. It follows the **contract** (which files, which cases) rather than the snippet, and says so in its own body. That difference is intentional and is the only region where its copy legitimately diverges.
