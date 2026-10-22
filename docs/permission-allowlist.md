# The manager permission allowlist — what a manager may answer on its own

**Anything not listed here goes to the operator.** This page is a closed list, not a
starting point: a command absent from it is not "probably fine", it is unlisted, and an
unlisted command is never answered by a manager. There is no wildcard entry, no
"obviously safe" class, and no entry a reader has to interpret to apply.

The list exists because `agents/goal-wrangler.md` decides routine approvals from the
worker's brief alone, and a brief is not a policy: the same command is answered differently
depending on how the task happened to be worded. A written list makes the answer the same
every time, and makes "the manager answered something it should not have" a decidable
question rather than a matter of reading a brief.

## What this list is not

It is **not** a sandbox, and it is **not** the shipped `server/policy.json`. The policy
decides what runs *without waking anyone*; this list decides what a **manager** may answer
when a prompt does reach one. Two consequences, both load-bearing:

- A command the policy already allows never reaches a manager, so listing it here is dead
  weight. `Read`, `Glob`, `Grep` and cwd-confined `Write`/`Edit` are answered by
  `server/policy.json` before `canUseTool` is called — they are **out of scope** for this
  page.
- Adding an entry here does **not** stop a prompt being raised. It only tells the manager
  what to answer when one is.

## The deny list — three classes a manager never answers

A command in any of these classes goes to the operator **even if it also matches an
allowlisted prefix**. The deny list is checked first and wins.

**prod** — anything that reaches a production system. Recognisable by, and not limited to: a
prod-cluster wrapper (`kubectlprod`, `kubectlnukeprod`), a `BRANCH=prod` or `*-prod`
worktree or path, a prod hostname or prod account name, or a command whose target the
worker's brief describes as prod. **When the environment is not stated, treat the command as
prod.** An unrecognised environment is never assumed to be staging.

**irreversible** — anything whose effect cannot be undone by re-running the task: a merge, a
release, a tag push, a force-push, a delete (`rm`, `kubectl delete`, `git branch -D`), a
deploy or restart of a running process, a message sent to another person, a write to an
external service. Reversibility is judged on the *effect*, not the verb — a `curl -X POST`
is irreversible if the endpoint acts on it.

**scope-changing** — anything that changes what the task is: creating or closing a task or
goal, editing a task's Success Criteria or Definition of Done, changing a policy file,
granting a permission, spawning or stopping a session, or widening this list. A manager
answers prompts; it does not decide what the work is.

## The list

Each entry names the tool, an **exact command prefix**, and its confinement. Under
`matchType: "command"` the prefix is a whole-token prefix — `ls` matches `ls -la /tmp` and
not `lsof` — and tokens after it are unconstrained, so an entry is included only when
**every** extension of it is read-only.

| Tool | Prefix | Confinement | Why every extension is safe |
|---|---|---|---|
| `Bash` | `ls` | any path | lists directory entries; no file content, no config, no execution |
| `Bash` | `cat` | any path | reads file content; cannot execute |
| `Bash` | `head` | any path | reads a prefix of a file |
| `Bash` | `tail` | any path | reads a suffix of a file (`-f` blocks, it does not act) |
| `Bash` | `wc` | any path | counts lines, words and bytes |
| `Bash` | `sort` | any path | reads and orders stdin or a named file |
| `Bash` | `uniq` | any path | filters adjacent duplicate lines |
| `Bash` | `diff` | any path | compares two files; `-r` walks directories, still read-only |
| `Bash` | `grep` | any path | matches patterns; cannot execute a match |
| `Bash` | `shasum` | any path | hashes file content |
| `Bash` | `md5` | any path | hashes file content |
| `Bash` | `jq` | any path | reads JSON; a filter file cannot start a process |
| `Bash` | `tree` | any path | lists a directory tree |
| `Bash` | `make test` | runner config unmodified | runs the committed test suite — see *Test runs* below |
| `Bash` | `bun test` | runner config unmodified | runs the committed test suite — see *Test runs* below |

That is the whole list. It is short on purpose — see the next section.

## Test runs — the one config-reading entry, and the check that makes it safe

`make test` and `bun test` read their runner configuration out of the working tree, which
every other excluded command below does too. They are allowlisted anyway, because they are
the command a worker asks for most often and a prompt for each one is most of what this
list exists to remove.

⚠️ **What makes them safe is the confinement, not the command.** Both entries carry
`runner config unmodified`, and the matcher must refuse the allow when any of `Makefile`,
`package.json`, or the lockfile carries an uncommitted change. That check is the whole
difference between "run this repository's committed test suite" and "run whatever is on
disk now" — and without it the entry is exactly as unsafe as the `git` exclusion below.

**`make` itself is still excluded.** Only the `test` target is listed: `make` matches a
whole-token prefix, so a `make` entry would also match `make buca`, `make apply` and
`make install`, none of which are read-only. `make test` as a prefix matches `make test`
and its arguments and nothing else.

**If the matcher cannot make that check, the entry comes off this list** and test runs go to
the operator. An entry whose confinement is not enforced is a wider entry than the page
claims, and this page's value is that a reader can trust it.

## What is deliberately excluded, and the rule that excludes it

⚠️ **The exclusion rule: a command that reads configuration or plugins out of the working
tree is not allowlistable on its own, because the shipped policy already lets a worker write
in its cwd.** The worker can therefore edit the file the command reads and have the command
run whatever it likes. This is the argument `README.md` § *Allowing a Bash command* makes
for `git`, and it is not a `git` special case — it is a property of the whole class. The
only command on this page that survives it is one paired with a confinement the matcher
enforces, and that is exactly the test-run case above.

| Excluded | Route |
|---|---|
| `git` (any subcommand, including `git status`) | `core.fsmonitor`, `core.pager`, `diff.external`, hooks — all named in `.git/config`, which the worker can write |
| `make` (any target **except** `test`) | reads `Makefile` from the working tree; `make test` is the one exception, and only under the clean-tree confinement above |
| `bun`, `npm`, `yarn`, `pnpm` | read `package.json` scripts and workspace config from the working tree |
| `node`, `python`, `python3`, `perl`, `ruby` | arbitrary code execution by definition |
| `sh`, `bash`, `zsh`, `env`, `xargs`, `parallel`, `time` | launch another program |
| `awk` | `system()` executes a shell command |
| `sed` | `-i` writes; a prefix cannot exclude it |
| `find` | `-delete` writes; a prefix cannot exclude it |
| `rm`, `mv`, `cp`, `tee`, `chmod`, `chown`, `ln`, `mkdir`, `touch` | mutate the filesystem |
| `curl`, `wget`, `ssh`, `scp`, `rsync` | reach a network target; the environment is unknowable from the prefix |
| `docker`, `kubectl`, `kubectl*` wrappers, `helm`, `terraform` | reach a cluster or runtime; prod cannot be ruled out from the prefix |

**Test runs are the one deliberate exception**, and the only entry on this page whose safety
rests on a confinement rather than on the command — see § *Test runs* above for the check
that admits them and for why `make` as a whole stays excluded.

## How a manager applies it

1. **Deny list first.** If the command is prod, irreversible or scope-changing, it goes to
   the operator. Stop.
2. **Then the list.** Match the command's token prefix against the table. A match is
   answered `allow`. A command that is not a whole-token prefix match is not a match —
   `lsblk` does not match `ls`.
3. **Everything else goes to the operator.** Not a deny — an escalation. A denial tells the
   worker it may not do the thing; escalation tells the operator it is their call, which is
   the truth for a command this page does not cover.
4. **Quote the entry.** The answer's reason names the entry that matched, so the record
   says *why* the manager allowed it, not just that it did.

⚠️ **A manager never answers a prompt it cannot place on this page.** "The brief probably
authorises it" is not a match. The brief is what the worker says it is doing; this page is
what the fleet has decided is safe.

Load this page, with the rest of `docs/`, via `/supervisor:read-guides`.
