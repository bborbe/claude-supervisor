# The manager permission tiers — what a manager answers, and what only the operator does

Every parked call has a **tier**, computed by one classifier (`scripts/permission-tier.py`) and
carried on the park record as `permission_tier`:

| Tier | Who answers | What it is |
|---|---|---|
| `allowlist` | the manager, without judgment | a match in [The list](#the-list) below — the 13 entries, unchanged |
| `manager` | the **owning manager**, on the rubric in [How a manager applies it](#how-a-manager-applies-it) | every call not in the operator-only class: writes, git reads, cluster reads |
| `operator` | **the operator, always** | the [operator-only class](#the-operator-only-class), plus anything the classifier cannot fully take apart |

This is the operator's ruling of 2026-11-06, in their words: *"read only => allowed · write =>
send to manager ... that is a second gate and can allow it · only if manager is unsure or is some
prod deploy stuff or so ... it should reach me always"*. Before it, everything off the list went
to the operator; now only the operator-only class does, and a manager decides the rest.

⚠️ **The tier routes the gate; the classifier never answers it.** `manager` means "a manager may
decide this", not "allow". `operator` is the only tier a manager must never answer, and it is
enforced by the classifier's negative cases (`server/permission-tier.fixtures.json`), not by the
manager's discretion.

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

## The operator-only class

The classifier checks **every segment** of a compound command (`;`, `&&`, `||`, `|`, `&`,
newline, subshell parens), recursing into `$(…)`, backticks, `<(…)` and `sh -c '…'`, scanning
heredoc bodies as text and splitting them as shell when they feed a shell. A call that matches any
form below is `operator` **even if it also matches an allowlist entry** — `cat ~/.netrc` matches
`cat` and is still `secret`. The `form` column is machine-read: `make check-manager-allowlist`
fails when it disagrees with the forms the fixture exercises.

| Form | What it covers |
|---|---|
| `prod` | `BRANCH=prod`, a `*-prod` path component, a prod cluster wrapper, `/nuke-deploy prod`, `make buca`/`apply`/`deploy` with no `BRANCH=dev`, `helm`/`terraform` mutations |
| `merge-to-protected` | `gh pr merge`, a push whose refspec targets `master`/`main` or `HEAD` |
| `push-unverifiable-target` | a `git push` with no refspec — its target is whatever upstream is set |
| `force-push/ref-delete` | `--force*`, `-f`, `--delete`, `--mirror`, a `+` or `:` refspec |
| `ref-delete` | `git branch -d/-D`, `git tag -d`, `git update-ref -d` |
| `irreversible-delete` | `rm` outside `/tmp/`, `git reset --hard`, `git clean`, `stash drop`, `find -delete`, `gh repo delete` |
| `cluster-mutation` | `delete`, `apply` without `--dry-run`, `create`, `patch`, `edit`, `exec`, `port-forward`, `cp` on a dev wrapper |
| `cluster-unknown-env` | bare `kubectl` or an unknown wrapper — the environment cannot be read from the command |
| `secret` | a credential path in any token or `file_path` (`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.kube`, `~/.config/gh`, `~/Library/Keychains`, `.netrc`, `.credentials.json`, `.teamvault.json`, `.git-credentials`, `.npmrc`, `.pypirc`, `id_*` keys, `*.pem`, `*.key`, any `.env*`, any `.claude.json`, `~/.claude` named as a directory), a whole-tree read of a broad directory (`grep -r`/`ls -R`/`find`/`du`/`tree`/`rg`, or a glob read, over `/`, `/Users`, `~`, or `~/.claude`/`~/.config`/`~/Library`), a secret CLI (`teamvault-cli`, `security find-*`), `kubectl get secret`, any `kubectl get -o yaml/json` |
| `permission-grant` | writing `~/.claude/settings*.json`, `git config --global` |
| `publish` | email/chat send, `gh release`, a tag push, `gh pr comment`/`review`, `gh api` writes, `curl` writes to a non-localhost URL, `docker push`, `npm publish` |
| `remote-host` | `ssh`, `scp`, `rsync` |
| `live-trade` | `trade-create`/`trade-close`, `trading-api-write`, a trading-prod MCP mutation |
| `service-restart` | `launchctl`/`systemctl` restarts — except `launchctl kickstart … com.bborbe.attention-watcher` |
| `privilege` | `sudo`, `doas`, `su` |
| `unverifiable` | an unterminated quote or heredoc (a truncated payload ends in one), a command word that is an expansion, `eval`, `source` |

⚠️ **Deliberate carve-out: `rm` confined to `/tmp/`.** Every target under `/tmp/` (no `..`) is a
worker's own scratch and goes to the manager; any other `rm` is `irreversible-delete`.

## The old deny list — the four classes the forms above implement

The classes below are the rationale; the forms above are what the classifier enforces. A manager
applying the rubric still treats them as its own red lines for anything the forms miss.

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

⚠️ **"Every extension" is a real test, and four commands failed it.** `sort` was on this list
until `-o FILE` was found to write an arbitrary path; `tree -o FILE` does the same. `uniq IN OUT`
writes `OUT` — an arbitrary-path write — and `diff -l` / `--paginate` runs `pr(1)`. All four are
**removed** rather than special-cased — a prefix rule cannot exclude a flag or a positional, so
the only honest fix is not to list the command. Any future entry whose extensions include an
output, exec or write-target argument has the same problem and must not be added without a
matcher that rejects those arguments.

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

Read the park's `permission_tier` (server workers: the request field; wrapper-backed workers:
`scripts/gate-answer-check.py`, which runs this classifier over the wrapper's full tool input
**and** the card payload and keeps the stricter — the store card itself carries no tier). Never
re-derive it by eye from the command text.

1. **`operator` → escalate. Stop.** Never answer it, whatever the brief says.
2. **`allowlist` → `allow`**, quoting the entry.
3. **`manager` → decide on the rubric.** Allow only when **all three** hold:
   - **(a) confined** — every write target is inside the worker's own cwd/worktree, or is its own
     feature branch or its own PR;
   - **(b) in the brief** — the worker's task names this work;
   - **(c) not scope-changing** — it does not create or close a task or goal, edit Success
     Criteria or DoD, change a policy file, grant a permission, or spawn/stop a session.

   ⚠️ **A script file is unseen code.** The classifier reads `sh -c '…'`, `python3 -c '…'` and
   shell heredocs, but never the body of a file: `bash /tmp/x.sh`, `python3 tool.py` and `./run`
   are `manager` tier with their content unchecked. Allow one only after reading the file and
   finding no operator-only form in it; otherwise it is "cannot verify".

   **Cannot verify (a), (b) or (c) → escalate to the operator.** Not a deny: escalation tells the
   operator it is their call, which is the truth for a call you could not place.
4. **Quote the tier and the rubric clause** in the answer's reason, so the record says *why*.

⚠️ **An over-eager manager tier is worse than a park.** A park reaches the operator; a wrong
allow hides the gate. When the rubric does not clearly hold, escalate.

### Releasing a wrapper-backed gate

A wrapper-backed worker (a `cc-*` launcher child, `mode: headless`) is parked inside
`claude-wrapper`, and **only the wrapper can release it**. A manager reaches it through the
**store card**, and that is the path `agents/gate-answerer.md` uses:

```
python3 scripts/attention-answer.py answer <store item id> --by <arm> --decision allow
```

The `claude-wrapper` daemon polls the store and settles the child's park with the card's
`decision` (`pkg/board.go`, `BoardDelivery`, carrier `claude-wrapper:board`), then writes the
delivery report `attention-answer.py` waits on — `ANSWERED` + `DELIVERY: delivered` only when it
landed, `RECORDED` + `NOT_DELIVERED`/`UNCONFIRMED` (exit 3) when it did not. It settles only a
`permission` card carrying `allow`/`deny`, whose `producer_id` is the parked child, whose session
is `headless`, and which does not predate the park it would settle. Measured 2026-11-18: of the last 60
answered permission cards in `GET /api/1.0/attention/history`, 8 carry a `delivered` report from
`claude-wrapper:board`.

The wrapper's own endpoint is the lower-level contract:

```
POST /api/1.0/sessions/<session-id>/answer   {"request_id": "<id>", "behavior": "allow"|"deny"}
→ 200 {"status": "answered"}
```

The endpoint is the contract; the wrapper listens on `localhost:18090`. ⚠️ **Take `request_id`
from `GET /api/1.0/sessions` (each row's `question.request_id`), never from the attention events
log or the feed.** Those print *store item ids*, a different namespace, and passing one to
`/answer` returns `409 no pending request`, which reads exactly like "already answered". This
plugin ships no caller; a manager's own one should validate both id shapes and fail loudly on a
non-2xx. `GET /api/1.0/sessions/<session-id>/question/<request_id>` reads one pending request.
⚠️ **A store answer is delivered only when its report says so.** An earlier version of this page
said a store answer on a headless permission card releases nothing; that predates the daemon's
board delivery. Read the delivery line, never the POST: `RECORDED` means the verdict is stored and
the worker may still be parked.

### The instant path, and when the manager does not act

**No gate is held off the board.** A wrapper-backed park posts its card immediately — the running
attention-controller registers no manager-hold producer (`main.go`: *"until then manager gates
post immediately"*), so an earlier claim here of a 10-minute hand-off hold described a design, not
the controller. The manager's answer therefore has to come fast or the operator answers first.

The fast path: every manager tier keeps its gate feed `Monitor` armed continuously and, on each
`hst:<item-id>` line, dispatches `supervisor:gate-answerer` in the background in that same turn
(`commands/steps/attention-watcher.md`). The answerer runs `scripts/gate-answer-check.py`, applies
the rubric above to what passed, and answers the card `allow` — or leaves it. A card it leaves
(`operator` tier, held, not this manager's worker, no readable brief, any clause it cannot verify)
stays on the board as the operator's call; the answerer never denies.

The check also refuses, mechanically: a `--self` that is not the running session's own
`CLAUDE_CODE_SESSION_ID`, or is not a session id at all; a `role: human` session; any tool outside
`Bash`, `Write`, `Edit`, `MultiEdit`, `Read`, `NotebookRead` (an `mcp__*` call, `WebFetch` or
`Agent` reaches something the classifier cannot read); a card whose tool differs from the park's; a
card older than its park, or either timestamp missing; any input cut at the 2000-char display cap;
a `Write`/`Edit`/`MultiEdit` whose target is outside the worker's cwd and `/tmp`, or under
`~/.claude`; and a `Bash` call that runs unseen code — an interpreter handed a file, a module or
stdin, any non-shell inline program, a shell `-c` body that is not itself allowlist-tier, a path
script, an unspaced `bash<x.sh`, and the launchers `xargs`, `find -exec`, `env -S`, `uv run`/`npx`
and `git -c alias.*`/`!`. `make`, package-manager scripts and `go run` stay `manager` tier on
purpose (they build the worker's own worktree; clause (a) still applies). Each manager keeps a
ledger of the items it checked (`~/.claude/state/gate-answer/<session>.json`; env overrides are
honoured only under `~/.claude/state`), so a feed re-arm never judges a card twice.

⚠️ **`--answer --expect <hash>` binds the judgment to the re-check, not the answer to the park.**
The store answer carries no `request_id` its carrier honours: claude-wrapper's
`BoardDelivery.answerFor` settles the producer's CURRENT park, refusing only a card created more
than 5 s before it. A worker that parks a new call between the re-check and the daemon's poll can
receive the allow. The end-to-end binding needs the request id carried through — the store's answer
route already accepts `request_id`; `board.go` must compare it with the park's before settling.

⚠️ **A permission answer's delivery can still claim the operator's voice.** Two carriers race on an
answered permission card. claude-wrapper's board delivery settles the park with the bare verdict
(`pkg/board.go`: `Answer{Behavior: decision}`, no text). attention-controller's watcher can carry
the same card first: for a pane-less session it hands the card to its headless arm, which writes a
turn opening *"The operator answered on the attention board."* with `allow` as the answer. Measured
2026-11-18 over the last 60 answered permission cards: of the 11 delivered to headless sessions, 7
were carried by `watcher:attention-watcher` and 4 by `claude-wrapper:board`. The verdict is right
either way; the attribution in that turn is the controller's to fix.

## A worker's closer pick — the second row the answerer takes

`PICK_ANSWERING_ENABLED = True` in `scripts/gate-answer-check.py` is the kill switch. The answer
is posted with `answer.answerer: manager`, and every carrier that delivers a card answer reads it:
attention-controller's pane carrier types `Manager answer, via supervisor: <label> [<id>]`
(`pkg/pane-carrier.go`), its headless arm opens the resumed turn with the same prefix
(`pkg/headless-answer.go` `AnswerTurnText`), and claude-wrapper's message arm delivers it under
`Manager answer, via supervisor:` (`pkg/board.go` `messageAnswer`) — never in the operator's voice.
⚠️ Those carriers need attention-controller v0.104+ and claude-wrapper v0.45+; an older carrier
ignores the field and voices the answer as the operator's, so set the flag to `False` if either
is rolled back.

A separate row from the permission tiers above, so the two are measured apart: a permission
answer is a `permission` card carrying `decision: allow`; a pick answer is a `message` card whose
`answer` is text starting `Manager answer, via supervisor:`. Both carry `answered_by:
supervisor:gate-answerer`, and the pick also writes `answerer_kind: manager` on the worker's
answer marker, so neither is counted as the operator's.

| Card | Answered with | Answered when |
|---|---|---|
| `answer_mechanism: message`, `interrupt_class: pick`, `producer_kind: session`, not a hook menu | its **one** recommended option (`recommended: true`, or a label carrying `(recommended`), as `Manager answer, via supervisor: <label>` | the worker is this manager's own or adopted, carries no operator hold, has exactly one brief, and the recommended option is work that brief already names and does not change its scope |

**Always left for the operator**, whatever the recommendation — `scripts/gate-answer-check.py`'s
`PICK_DENY`, matched over the payload, the context and every option label: prod or a deploy, a
merge or release (a tag, a publish), a delete, a force push, secrets or credentials, a live trade,
a TDR, any `approve:`. Also left: an `approve`-class card, a pick with no recommended option or
with several, a hook menu (an `AskUserQuestion` modal — answered by keystroke, never by text), a
pick whose producer is not a worker session, and any pick whose scope the answerer cannot tie to
a line of the task file. *Not scope-changing* means what rubric clause (c) above means, plus: it
does not abort, complete, hold or defer the task.

The answer goes through the same store-card path — `gate-answer-check.py --answer`, which
re-checks and then runs `attention-answer.py answer <id> --answer-text "Manager answer, via
supervisor: <label>" --answerer manager`. `attention-answer.py` refuses `--answer-text` while
`PICK_ANSWERING_ENABLED` is false, refuses any answerer other than `operator` / `manager`
(`--answerer` has no default), and refuses the `supervisor:gate-answerer` arm unless it answers as
`manager`. The attention-controller carries it (carrier `watcher:attention-watcher`: typed into a
tab worker's pane, or a resumed turn into a headless one); measured 2026-11-18, of the store
history's 21 answered pick cards 17 carry a `delivered` report from that carrier (1 `failed`, 3
none).

Load this page, with the rest of `docs/`, via `/supervisor:read-guides`.
