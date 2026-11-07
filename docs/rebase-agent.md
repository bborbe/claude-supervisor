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

## The scratch worktree

```
git worktree add --detach <tmp> <head-sha>   # detached: no branch ref exists to damage
git -C <tmp> rebase origin/master
git -C <tmp> update-ref refs/rebase-agent/pr/<n> HEAD
git -C <tmp> worktree remove --force <tmp>   # in the repo, after the ref is written
```

The main worktree is never entered and no local branch is ever created. The push line maps
the throwaway ref onto the live branch, with the lease pinning the old SHA:

```
git -C <repo> push --force-with-lease=refs/heads/<branch>:<old-sha> \
  origin refs/rebase-agent/pr/<n>:refs/heads/<branch>
```

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
