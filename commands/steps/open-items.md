# Act on the open-items ledger

When: the ledger holds ≥1 open entry

## The open-items ledger — the operator's asks

Not a fifth channel: it holds what the operator asked and is still waiting on — the stretch before a task exists, and between a question and its answer. Read and write it only through the **`supervisor:open-items` skill** — `/supervisor:open-items <subcommand> …`, the single home of its kinds, its subcommands and its rules; never restate them here. ⚠️ **The subcommands are deliberately NOT enumerated on this line** — the copy that stood here had **already** drifted, omitting `classify`, which the skill's `argument-hint` has carried all along; the skill's `argument-hint` is the list. The fleet layer's specifics:

- **Read at round start** (Step 0b, in the digest), **render every round** under `📋 Open with the operator`.
- **Three close paths, and only these:** a task file's `status: completed` read *this round* (Step 4), through `close --evidence`; the operator's own answer, through `answer`; or the operator's withdrawal, through `withdraw`, for an entry that should not be outstanding — filed in error, superseded, or never real. ⚠️ **The skill's kind table is the single home of which path applies to which kind** — this line is a pointer, not a copy, and it is not the place to add a fourth.
