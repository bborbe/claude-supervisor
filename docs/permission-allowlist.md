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

**credentials** — anything that reads or moves a secret, a key, a token or a credential
store: `~/.ssh`, `~/.aws`, `~/.config/gh`, `.env` files, a keychain or TeamVault read, a
`cat`/`grep`/`head`/`tail` aimed at any of those paths, or any command whose target is a
password manager. ⚠️ **This class exists because the read-only entries below take any path.**
`cat` is on the list and `cat ~/.ssh/id_rsa` matches it — the class is what stops that, and
a manager that answers it has leaked a key. When a path could be a credential store, treat
it as one.

## The list

Each entry names the tool, an **exact command prefix**, and its confinement. Under
`matchType: "command"` the prefix is a whole-token prefix — `ls` matches `ls -la /tmp` and
not `lsof` — and tokens after it are unconstrained, so an entry is included only when
**every** extension of it is read-only.

⚠️ **"Every extension" is a real test, and two commands failed it.** `sort` was on this list
until a review caught that `sort -o FILE`, `sort --output=FILE` and
`sort --compress-program=PROG` all match the prefix and write a file or run a program;
`tree -o FILE` does the same. Both are **removed** rather than special-cased — a prefix rule
cannot exclude a flag, so the only honest fix is not to list the command. Any future entry
whose extensions include an output or exec flag has the same problem and must not be added
without a matcher that rejects those flags.

| Tool | Prefix | Confinement | Why every extension is safe |
|---|---|---|---|
| `Bash` | `ls` | any path | lists directory entries; no file content, no config, no execution |
| `Bash` | `cat` | any path | reads file content; cannot execute |
| `Bash` | `head` | any path | reads a prefix of a file |
| `Bash` | `tail` | any path | reads a suffix of a file (`-f` blocks, it does not act) |
| `Bash` | `wc` | any path | counts lines, words and bytes |
| `Bash` | `uniq` | any path | filters adjacent duplicate lines |
| `Bash` | `diff` | any path | compares two files; `-r` walks directories, still read-only |
| `Bash` | `grep` | any path | matches patterns; cannot execute a match |
| `Bash` | `shasum` | any path | hashes file content |
| `Bash` | `md5` | any path | hashes file content |
| `Bash` | `jq` | any path | reads JSON; a filter file cannot start a process |
| `Bash` | `make test` | `exact`, `clean-runner-config` | runs the committed test suite — see *Test runs* below |
| `Bash` | `bun test` | `exact`, `clean-runner-config` | runs the committed test suite — see *Test runs* below |

The **Confinement** column is machine-read, not prose: `make check-manager-allowlist` parses
it and fails when it disagrees with `server/manager-allowlist.json`. Write the machine name in
backticks (`exact`, `clean-runner-config`) or `any path` — anything else is a shape error.

That is the whole list. It is short on purpose — see the next section.

## Test runs — the one config-reading entry, and the check that makes it safe

`make test` and `bun test` read their runner configuration out of the working tree, which
every other excluded command below does too. They are allowlisted anyway, because they are
the command a worker asks for most often and a prompt for each one is most of what this
list exists to remove.

⚠️ **Two things make them safe, and both are required.**

**1. The command must be exact.** Both entries carry `exact: true`, so the match is only
`make test` or `bun test` **with no further tokens**. A bare prefix would not do: the
arguments select the code that runs — `make test -f evil.mk`, `bun test ./evil.test.ts`,
`bun test --preload ./evil.ts` all start with the prefix and run something the worker wrote.

**2. The runner config must be unmodified.** The matcher refuses the allow when any file the
runner actually reads carries an uncommitted change: `GNUmakefile`, `makefile`, `Makefile`,
`package.json`, `bunfig.toml`, and the lockfile. ⚠️ **Every one of them, not just
`Makefile`** — `make` prefers `GNUmakefile` over `Makefile`, and `bun` reads `bunfig.toml`
for preloads, so checking only the canonical name leaves a worker free to write the file that
is read *instead*. That is the whole difference between "run this repository's committed
test suite" and "run whatever is on disk now".

**`make` itself is still excluded.** Only the `test` target is listed: a `make` entry would
also match `make buca`, `make apply` and `make install`, none of which are read-only.

⚠️ **Residual risk, stated rather than hidden:** the confinement compares *uncommitted*
state, so a worker that could commit an edited `Makefile` would pass it. It cannot — `git`
is excluded here and escalated by the policy — but the check rests on that, not on itself.

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
