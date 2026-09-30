---
description: "The operator's approval inbox — every task at phase: todo, and the three verbs that move one"
allowed-tools:
  - Bash(python3:*)
  - Bash(vault-cli task approve:*)
  - Bash(vault-cli task set:*)
argument-hint: "[approve|reject|later] <task> [reason]"
---

`phase: todo` is the **operator's approval boundary**: agents file there and stop,
and the operator's `todo → planning` flip *is* the approval. This is the read
surface for that boundary, plus the three verbs that move one row across it.

⚠️ **The command never picks a row. The operator names it.** Every verb below
takes a task the operator typed. `reject` is irreversible and `approve` is the
only thing that releases work to the fleet, so a command that chose its own row
would be deciding what gets built — the exact inversion the boundary exists to
prevent. There is no "approve everything", no filter, and no recommended row.

⚠️ **`vault-cli task approve` is the only approval.** Never substitute
`vault-cli task set <task> phase planning`: that writes the phase and nothing
else, so the row moves while `approved_by` / `approved_at` stay empty and the
approval is recorded as never given. `approve` writes all three in one storage
call. The same reason bars vault-ui's `PATCH /api/tasks/{id}/phase`.

## No arguments — the view

Run exactly:

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/inbox.py
```

Print the output verbatim, then stop. It is read-only: it writes no task file,
no cache and no state file, so it is always safe to re-run.

Each row prints its vault in brackets. **That is the identity, not decoration** —
task titles are not namespaced across vaults, and the same title can be live in
two vaults at once.

## `approve <task>` / `reject <task> <reason>` / `later <task>` — the verbs

**Resolve the vault first — every verb, no exception.** Ten vaults declare a
`tasks_dir`, so the configured default is one of them and the wrong one writes
the wrong vault. Run:

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/inbox.py --resolve "<task>"
```

- Prints the vault name → use it as `<vault>` below.
- `rc=2` → **fatal, and not an empty inbox.** vault-cli's config is unreadable, or
  no vault declares a `tasks_dir`. Report the stderr line verbatim and stop —
  rendering here would show a clean queue over a scan that never ran.
- `rc=3` (no live row) → report verbatim and stop. The row is approved, rejected,
  deferred, or misspelled; it is not this command's job to guess which.
- `rc=4` (ambiguous) → report the vaults and stop. Ask the operator which one;
  never pick.

⚠️ **Never hand-write the vault.** Resolving by scanning for the title yourself
re-implements the ambiguity refusal above and drops it — the failure is silent
and moves a row in a vault the operator never named.

### approve

```bash
vault-cli task approve "<task>" --vault "<vault>"
```

Writes `phase: planning`, `status: next`, `approved_by` and `approved_at` in one
storage call. Report the row's new phase. The operator's own words are the
approval — this command relays it, it does not decide it.

### reject

```bash
vault-cli task set "<task>" status aborted --reason "<reason>" --gate-successor "<successor|none>" --vault "<vault>"
```

⚠️ **Both `--reason` and `--gate-successor` are required, and the CLI enforces
it.** `aborted` is not "aborted with a reason": the successor names the task,
alert or owner that inherits the row's risk gate, or the literal `none`. If the
operator gave a reason but no successor, **ask which** — do not default to
`none`, because `none` is a claim that nothing inherits the risk, and that claim
is the operator's to make.

⚠️ **Reject is irreversible from this surface.** There is no reopen verb; a
mis-rejected row needs the three-command reopen sequence from `/vault-cli:execute-task`.
Read the task name back before running it.

### later

```bash
vault-cli task set "<task>" status backlog --vault "<vault>"
```

⚠️ **`later` writes `status`, never `phase`.** `backlog` keeps the row out of the
inbox (the view excludes it) while leaving `phase: todo` intact, so the approval
boundary is untouched and the row can be approved later without a second
migration. Setting the phase instead would read as an approval.

## After any verb

Re-render (`inbox.py`, no arguments) and show the new count, so the operator sees
the row leave the queue. If the count did not move, the verb did not land — say
so rather than reporting success.
