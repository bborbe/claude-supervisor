# The rebase agent

`scripts/rebase-agent.py` rebases every open PR whose branch is behind master, verifies the
result, and hands the operator **one push line per PR**. It never merges and it never pushes.

## Why it exists

A release cut renames the top `## Unreleased` to `## vX.Y.Z`, and every open branch still
carries `## Unreleased`. Each merge therefore re-conflicts every other open PR. Measured
2026-11-06 in `bborbe/claude-supervisor`: **8 of 12 open PRs DIRTY, 4 of them APPROVED**,
and four merges that evening re-broke the set four times. The conflict is cheap to
reconstruct — the cost is that nobody is doing it.

⚠️ **The trap is the mechanism, not the conflict.** "Sync the branch with master" naturally
means `git merge origin/master`, and that writes a merge commit. Measured on `#652` the same
evening: three merge commits (`4ead1e56`, `da40e840`, `c1e51e25`), head APPROVED, PR
`MERGEABLE`/`CLEAN`. Nothing in the PR's own state signalled a problem — it would have
merged cleanly and passed every visible check while permanently failing the merge train's
linearity probe. This agent rebases, and the linearity gate is what proves it.

## What already detected this

`github-pr-watch/scripts/watch.sh` reads `mergeStateStatus == "BEHIND"` and
`mergeable == "CONFLICTING" || mergeStateStatus == "DIRTY"` from `gh pr view` — per PR,
warn-only, and its own remedy line prescribes `git merge origin/<base>`, the exact mechanism
this agent exists to replace. Nothing sweeps a queue. The GitHub fields are reused as the
signal; only the queue-level sweep and the rebase are new.

## The repo set, and why not "every repo"

The conflict class exists only where a release cut renames `## Unreleased` in a `CHANGELOG.md`
that open branches also edit. A repo without that convention cannot produce the conflict, so
sweeping it would rebase branches for nothing. The set is a constant in the script:

| Repo | Why it is in the set |
|---|---|
| `bborbe/claude-supervisor` | the measured queue — 13 open PRs, 8 `CONFLICTING`/`DIRTY` on 2026-11-07 |
| `bborbe/vault-cli` | the second repo in the set, and it holds the exact pathology: two open PRs, both `CONFLICTING`, both APPROVED |

## The poll trigger

`launchd/com.bborbe.rebase-agent.plist`, `StartInterval` **900** — the fleet's own round
interval. A slash command cannot satisfy "detected within one poll interval with no human
starting the run", because it is human-started by construction.

```bash
cp launchd/com.bborbe.rebase-agent.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.bborbe.rebase-agent.plist
```

Output goes to `~/.local/state/claude-supervisor/rebase-agent/latest.json` (plus one
timestamped copy per run) and to `~/Library/Logs/rebase-agent.log`.

### The run record

Every run prints one `record: {json}` line to stdout beside the human render, and writes
the same payload to the state directory. The line is the record that survives a
container: when the agent runs as a cluster CronJob the state directory is an `emptyDir`,
so the pod's log is all a later reader has — `kubectl logs job/<name> | grep '^record: '`.

```json
{"agent":"rebase-agent","generated_at":"2026-11-13T14:00:12+00:00","trigger":"schedule",
 "state":"/state/2026-11-13T14-00-12Z.json","errors":[],
 "rows":[{"repo":"bborbe/claude-supervisor","pr":123,"verdict":"PUSH",
          "transfer":{"kind":"pvc-bundle","target":"rebase-agent-bundles:/bundles/pr-123.bundle"},
          "push_line":"kubectl… cp … && git -C … fetch … && git -C … push …"}]}
```

`--trigger` (default `manual`) is written into the payload, so a run started by hand is
never readable as a scheduled one. A cluster CronJob passes `--trigger schedule`; every
other caller gets `manual` without saying anything.

The record names the rows it rebased and the push line for each — never a `DIRTY → CLEAN`
transition, because the agent never pushes. The PR stays `DIRTY` until the operator
applies that line.

## The board

A record nobody reads is not delivery. Each `PUSH` row is also posted to the fleet
attention board as one card whose body carries the line **verbatim**, so the operator gets
it without a `kubectl logs` on a pod that no longer exists.

The write is guarded by two variables and is off when they are absent:

| Variable | Meaning |
|---|---|
| `ATTENTION_STORE_URL` | base URL of the store's bearer-gated listener — `http://attention-controller.agent.svc.cluster.local:18081` from the cluster |
| `ATTENTION_STORE_TOKEN` | bearer token, required for any non-loopback store |

⚠️ **The port is 18081, not 18080.** 18080 serves the unauthenticated board page; 18081
serves only `/api/1.0/attention…` behind the token. Pointing this at 18080 would publish
the card-writing path on the surface the board itself lives on.

⚠️ **No default.** The session producers default to `localhost:18080`; this one does not,
because the same script runs on the Mac and a default would post every sweep to whatever
happens to listen there. An unset store prints one line on stderr — only when the run has a
push line to lose — and leaves the push line in the run record.

**Where the variables are set:** nowhere in this repo. The cluster CronJob lives in
`bborbe/nuke` (`rebase-agent/rebase-agent-cronjob.yaml`), and that unit carries both — the
URL as a literal and the token from its own Secret. Set them there, not here; this script
reads the environment and nothing else.

**Store floor:** `attention-controller ≥ v0.42.0`. That is the release carrying `owner:`
liveness and `expires_at` enforcement; against an older store the card's liveness model is
unknown and the push is rejected, which surfaces as a `board_error` naming the store's
message.

### The card's declaration, and why each field is what it is

| Field | Value | Why |
|---|---|---|
| `producer_id` | `rebase-agent` | Stable across runs — the dedup key is compared *within a producer*. |
| `producer_kind` | `cron` | The closed enum's member for a scheduled job. |
| `provenance_class` | `explicit` | An explicit producer call carrying a stable id. |
| `answer_mechanism` | `ack` | A condition report: the operator acts on it, nothing routes back to the pod. |
| `interrupt_class` | `approve` | Self-report, not enumerated by the store; the rung the watcher's own ack cards use. |
| `dedup_key` | `rebase-agent:<repo>#<n>` | Per PR, so a sweep that keeps finding the same branch refreshes one card. |
| `liveness_ref` | `owner:operator` | See below. |
| `expires_at` | now + 1 h | The card's only removal, given `owner:` probes nothing. |

⚠️ **`owner:` is not a convenience — the other two models break the card.** Duplicate
suppression only collapses a re-push while the store reads the producer as live
(`updateExistingIfLive` → `isProducerLive`), and it resolves `heartbeat:<path>` against the
**store host's** filesystem and `session:` against the store host's session registry. A pod
appears in neither, so both read *gone*, suppression never applies, and every 15-minute
sweep adds a **new** card — a board that floods instead of one that reports. `owner:`
probes nothing and returns live unconditionally, which is exactly why it must be paired
with `expires_at`: an unbounded `owner:` card would never leave.

⚠️ **A board failure is never a rebase failure.** The post runs outside the per-PR `try`,
so an unreachable store cannot turn a good rebase into an `ERROR` verdict; the failure is
recorded as `board_error` on the row and printed to stderr, and the record still carries
the line.

## The scratch worktree

```
git worktree add --detach <tmp> <head-sha>   # detached: no branch ref exists to damage
git -C <tmp> rebase origin/master
git -C <tmp> update-ref refs/rebase-agent/pr/<n> HEAD
git -C <tmp> worktree remove --force <tmp>   # in the repo, after the ref is written
```

The main worktree is never entered and no local branch is ever created. The rebased commit
is published as a **range bundle** — `origin/master..refs/rebase-agent/pr/<n>` — to the
transfer volume, and the operator's line copies it off the reader pod, fetches it, and
pushes in one paste:

```
kubectlWRAPPER cp <reader-pod>:/bundles/pr-<n>.bundle /tmp/rebase-agent-pr-<n>.bundle \
  && git -C <operator-path> fetch /tmp/rebase-agent-pr-<n>.bundle \
       +refs/rebase-agent/pr/<n>:refs/rebase-agent/pr/<n> \
  && git -C <operator-path> push --force-with-lease=refs/heads/<branch>:<old-sha> \
       origin refs/rebase-agent/pr/<n>:refs/heads/<branch>
```

⚠️ **`<operator-path>` is the Mac's checkout, never the pod's.** `path` in `DEFAULT_REPOS`
is `expanduser`d and resolves to `/workspace/…` under the CronJob's `HOME`; `operator_path`
is a literal and is the one the line names. A line naming the pod's clone cannot be pasted
anywhere.

⚠️ **`&&`, not three steps.** The operator must paste one line and do nothing else; a copy
that succeeded while the fetch failed must stop before the push.

⚠️ **The fetch is forced (`+`).** The Mac's scratch ref survives between lines, so a
re-sweep fetches a newer commit onto an older ref, which a plain refspec refuses.

The transfer needs three deployment facts, all from the environment — a pod cannot infer
which `kubectl` wrapper the operator's Mac has, and a wrong guess produces a line that
fails only after it has been pasted:

| Variable | Meaning |
|---|---|
| `REBASE_BUNDLE_DIR` | directory on the PVC the pod writes bundles to, e.g. `/bundles` |
| `REBASE_KUBECTL` | the operator's wrapper, e.g. `kubectlnukeprod` |
| `REBASE_READER_POD` | the long-lived pod that mounts the same PVC |

Any of the three unset **refuses the row** before the rebase runs — it never emits a line
that cannot work.

`refs/rebase-agent/` is shared across every worktree of the repo, so the line works from the
main checkout.

## Conflict policy

| Conflict in | Action |
|---|---|
| any path other than `CHANGELOG.md` | **refuse**, name the paths — a genuine content conflict is a worker's |
| `CHANGELOG.md` only, branch adds bullets only | resolve mechanically: take master's side, insert the branch's own bullets under `## Unreleased` |
| `CHANGELOG.md` only, branch does more than add bullets | **refuse** — reconstruction would silently discard that edit |

The mechanical resolution never guesses: the bullets come from the branch's own commits
(`git diff origin/master...<head> -- CHANGELOG.md`), never from a working tree.

## The gates — blockers, not warnings

A failed gate produces a **failure report instead of a push line**.

1. `git log --merges origin/master..HEAD` is empty — the branch is linear.
2. `git diff origin/master -- CHANGELOG.md | grep -E '^[-+]## '` prints only `+## Unreleased`.
   ⚠️ An **empty** result is ambiguous between "no heading moved" and "already under
   Unreleased", so it is never read as a pass on its own.
3. `git diff origin/master -- CHANGELOG.md | grep -E '^--[^-]'` is empty — no bullet master
   carries was dropped. ⚠️ The pattern is `^--[^-]`, not `^-[^-]`: a removed bullet renders as
   `-- <bullet>`.
4. The `awk` placement assertion prints `sits under: ## Unreleased` for every bullet the
   branch adds — **the authority**, and what disambiguates (2).
5. `grep -c '^## Unreleased$' CHANGELOG.md` prints **1** — a duplicate heading reads as one
   to gates 2 and 4, since inserting an identical heading above master's is a single added
   line.
6. The PR stays under the repo's park size limit. ⚠️ The limit is the auto-reviewer's
   `MAX_ADDITIONS` / `MAX_CHANGED_FILES` **deployment env**, not repo config, so the values
   in the script are the observed defaults and are **unverified** against the running watcher.

The `awk` form is shared with the coding plugin's `check-changelog-unreleased-fold.sh`,
including its `ENVIRON` trick — `awk -v` processes escape sequences in the bullet, so a
literal backslash-n arrives as a newline, matches no line, and reports a false MISSING. That
guard is **not** called from here: it predicts what a *merge* would fold, and no merge happens
on this path.

## What is out of scope by contract

- **Merge ordering.** The agent rebases; it never decides what merges when. `#652` must merge
  last in the permission train regardless of how clean it looks.
- **Prod-pin PRs.** A PR that changes a prod pin is the operator's approval. Refused by path
  and by branch/title hint before any rebase.
- **Resolving genuine content conflicts.** Detect and refuse; a worker resolves.

## Exercised on the live queue, 2026-11-07

| Repo | PR | Behind | Outcome |
|---|---|---|---|
| claude-supervisor | 664 | 22 | PUSH — started with 1 merge commit, came out linear |
| claude-supervisor | 653 | 134 | PUSH — started with 1 merge commit, came out linear |
| claude-supervisor | 651 | 167 | PUSH |
| claude-supervisor | 643 | 193 | PUSH |
| claude-supervisor | 611 | 371 | PUSH |
| vault-cli | 336 | 146 | PUSH |
| claude-supervisor | 660 | 17 | REFUSED — clean rebase lost `v0.250.2` / `v0.250.1` |
| claude-supervisor | 521 | 979 | REFUSED — bullet lands under `## v0.210.0` |
| claude-supervisor | 516 | 83 | REFUSED — bullet lands under `## v0.209.1` |
| vault-cli | 354 | 30 | REFUSED — content conflict in `scripts/session-self-exit.py` |

**No push was executed by the agent.** Every line was handed to the operator.
