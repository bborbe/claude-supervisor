---
description: "The operator's approval inbox — every task at phase: todo, and the three verbs that move one"
allowed-tools:
  - Bash(python3:*)
  - Bash(vault-cli task approve:*)
  - Bash(vault-cli task set:*)
argument-hint: "[approve|reject|later] <task> [reason]"
---

`phase: todo` is the **operator's approval boundary**: agents file there and stop, and
the operator's `todo → planning` flip *is* the approval. This is its read surface, plus
the three verbs that move one row across it.

⚠️ **The command never picks a row; the operator names it.** `reject` is irreversible
and `approve` releases work to the fleet, so a command choosing its own row would be
deciding what gets built — the inversion the boundary exists to prevent. There is no
"approve everything", no filter, and no recommended row.

⚠️ **`vault-cli task approve` is the only approval.** Never substitute
`vault-cli task set <task> phase planning` — it writes the phase and nothing else, so
the row moves while `approved_by` / `approved_at` stay empty and the approval reads as
never given. The same reason bars vault-ui's `PATCH /api/tasks/{id}/phase`.

## No arguments — the view

Run exactly:

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/inbox.py
```

Print the output verbatim, then stop. It is read-only — no task file, cache or state
file — so it is always safe to re-run.

Each row prints its vault in brackets — **that is the identity, not decoration**, since
titles are not namespaced across vaults and the same title can be live twice.

## `approve <task>` / `reject <task> <reason>` / `later <task>` — the verbs

**Resolve the vault first — every verb, no exception.** Ten vaults declare a `tasks_dir`,
so the configured default is one of them and the wrong one writes the wrong vault. Run:

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/inbox.py --resolve "<task>"
```

- Prints the vault name → use it as `<vault>` below.
- `rc=2` → **fatal, and not an empty inbox.** vault-cli's config is unreadable, or no
  vault declares a `tasks_dir`. Report the stderr line verbatim and stop — rendering
  here would show a clean queue over a scan that never ran.
- `rc=3` (no live row) → report verbatim and stop. The row is approved, rejected,
  deferred, or misspelled; it is not this command's job to guess which.
- `rc=4` (ambiguous) → report the vaults and stop. Ask the operator which one; never pick.

⚠️ **Never hand-write the vault** — resolving by scanning for the title re-implements the
ambiguity refusal above and drops it, silently moving a row in a vault never named.

### approve

```bash
vault-cli task approve "<task>" --vault "<vault>"
```

Writes `phase: planning`, `status: next`, `approved_by` and `approved_at` in one storage
call. Report the new phase — this relays the operator's approval, it does not decide it.

### reject

```bash
vault-cli task set "<task>" status aborted --reason "<reason>" --gate-successor "<successor|none>" --vault "<vault>"
```

⚠️ **Both `--reason` and `--gate-successor` are required, and the CLI enforces it.** The
successor names the task, alert or owner inheriting the row's risk gate, or the literal
`none`. If the operator gave a reason but no successor, **ask which** — never default to
`none`, because `none` claims nothing inherits the risk, and that claim is theirs to make.

⚠️ **Reject is irreversible here** — there is no reopen verb; a mis-rejected row needs the
three-command reopen sequence from `/vault-cli:execute-task`. Read the name back first.

### later

```bash
vault-cli task set "<task>" status backlog --vault "<vault>"
```

⚠️ **`later` writes `status`, never `phase`.** `backlog` keeps the row out of the inbox
(the view excludes it) while leaving `phase: todo` intact, so the boundary is untouched
and the row can be approved later. Setting the phase instead would read as an approval.

## After any verb

Re-render (`inbox.py`, no arguments) and show the new count, so the operator sees the row
leave the queue. If the count did not move, the verb did not land — say so rather than
reporting success.
