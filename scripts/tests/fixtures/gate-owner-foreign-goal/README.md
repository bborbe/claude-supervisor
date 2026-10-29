# gate-owner-foreign-goal — the five measured foreign firings, as a replayable fixture

Goal manager `49b471f1-a8a6-48b0-b2b2-14b9cb2437e3` (goal *Managers Unblock Their Own Workers
and Approve Routine Permissions Within Policy*) was handed **five gates raised by five other
sessions in four other goals** in one afternoon on 2026-10-29, each resolved by hand to a task
whose `goals:` names a different goal. `gate-owner-filter.py` had no way to say so: its
ownership model is the spawn edge plus liveness, so its only answers were a liveness-derived
drop or an emit, and neither names an owner.

## The five cases

| pane | session | owning task | owning goal |
|---|---|---|---|
| 269 | `695c311f` | SC7 Has No Scorer and No Owner, So No Stuck Record Can Ever Be Counted | `PR Reviews Return a Verdict Within Minutes, Not Hours` |
| 260 | `0eacc5e4` | BRO-22423 Check Builds | `All Octopus Builds Green on Dev, Staging, and Master` |
| 272 | `2767ba96` | Vault UI Topic Cards Contradict Their Own Column and Show No Live State | *(none declared)* → `owner-unknown` |
| 278 | `0b7d28a7` | Give the Board-Drain Template Success Criteria and Tasks Sections | `Every Recurring Task Is Structurally Valid and Linked to a Runbook` |
| 279 | `51e78933` | Port weekly-strategy-rep W42 and W43 Fixes Into the Schedule YAML | *(none declared)* → `owner-unknown` |

Pane **410** is the negative control: a pane spawned by `--self`, whose task is in the goal's
own `# Tasks`. A fixture that excludes everything would look identical to a working one
without it.

⚠️ **The task file's own table is wrong on two rows, and the vault is the authority.** It
lists panes 278 and 279 as "none declared" and 269 as belonging to *A Wrapper-Spawned Session
Is Drivable Without a Human Keystroke*. The frontmatter says otherwise: `695c311f` stamps
*SC7 Has No Scorer* (`PR Reviews…`), `0b7d28a7` stamps *Give the Board-Drain Template*
(`Every Recurring Task…`), and `51e78933` stamps *Port weekly-strategy-rep* which declares no
goal. That is why three cases carry a goal and **two** are `owner-unknown` — the count SC3
states.

## What the stubs reproduce

`registry/` holds **only** this manager, so every foreign spawner reads `dead-manager`. That is
the firing-time shape: the filter emits a foreign gate whenever the live chain does not resolve
— a dead spawner, an unresolved `parent_session`, or an unreadable ledger all land on `EMIT`.
The same five panes read `peer-manager` on a later live run, which is precisely why the fix
keys on the vault rather than on that chain.

`ledger.json` is served by `endpoint_fixture.FixtureEndpoint` (the same stub the unit suite
uses), and `state/` + `registry/` + `claims.json` are the remaining stubs the filter takes as
flags. `vault/` is the scope input: one goal page (`manager_session_id` + `# Tasks`) and the
five task pages, each stamped exactly as its real page is — including pane 260's, which is
stamped only through a `- session_id:` metrics entry.

## Replay

```bash
python3 scripts/tests/fixtures/gate-owner-foreign-goal/replay.py                # fixed build
python3 scripts/tests/fixtures/gate-owner-foreign-goal/replay.py --scope off    # no scope input
python3 scripts/tests/fixtures/gate-owner-foreign-goal/replay.py \
  --script ~/.claude/plugins/cache/claude-supervisor/supervisor/<ver>/scripts/gate-owner-filter.py
```

⚠️ `--scope off` is not just "omit `--scope-vault`": that flag only overrides the default
source, so omitting it still reads `vault-cli`'s config and resolves the live vaults. The
runner passes `--no-scope` when the target copy has it, and omits both for a copy that
predates the input.

## Readings

Before (`6cb117fb`, the build the five firings came from):

```
feed stdout:  269 260 272 278 279 410
gates: 6  emit: 6  dropped(peer-manager): 0  dropped(peer-manager-own): 0  dropped(claimed): 0
```

After (this tree):

```
feed stdout:  410
gates: 6  emit: 1  dropped(peer-manager): 0  dropped(peer-manager-own): 0  dropped(claimed): 0
          excluded(foreign-goal): 3  excluded(owner-unknown): 2
```

and `--explain --json` still carries **all six** rows — the five as `exclude` with
`foreign-goal:<goal>` / `owner-unknown`, the control as `emit own-worker`. A pane missing from
`--json` would be a FAIL: a row that disappears from a sweep is indistinguishable from a row
that got fixed.
