# Attention and delegation — how a manager hands operator work to the operator

The single home for what a manager does when it finds work **only the operator can do** inside a task it drives. [`role-mode-matrix.md`](role-mode-matrix.md) decides a task's `role` from its body (Rule 1) and how a `role: human` row opens (Rule 4); its **Rule 5** carries the size test and points here. This file explains the shapes the test routes to, the channels each uses, and who closes each. It cites the matrix and [`fleet-surface.md`](fleet-surface.md) by section and restates neither.

Operator ruling, 2026-11-20: *"if it's very small, maybe the attention board card is fine, but for everything bigger that the manager wants to delegate to the human, we create dedicated tasks."*

## 1. What attention is

**Attention** is one answer the operator gives, once, to a question a session cannot settle itself. It costs the operator a read and a click, and it leaves nothing behind but the answer.

Example: a worker asks *"merge PR #41 now that CI is green, or wait for the Friday batch?"* — the operator picks an option on the board and is done.

## 2. What a human task is

A **human task** is a vault row with `role: human` (matrix Rule 1): work that needs the operator's hands, presence, or a judgement reserved to them, and that **takes more than one answer or leaves an artifact** — a registration, a created account, a stored credential, a signed document. It is tracked, it carries its own Success Criteria, and it opens as the operator's own session (matrix Rule 4).

Example: re-registering a Billomat account — a web login, a form, a mail confirmation, and a new account id to record.

## 3. What an agent task is

An **agent task** is a `role: agent` row (matrix Rule 1's default), run headless (matrix Rule 2). Its body describes work an agent finishes with commands. It may *need* operator input — that input is either attention (§ 1) or a separate human task (§ 2), never an unowned box inside the agent row (§ 6).

Example: routing Billomat invoices to the accounting mailbox — config, deploy, verify — once the account exists.

## 4. The decision table

The test the **manager** applies when it finds operator work inside a row it drives:

> **Does the operator finish it with one answer (about 2 minutes), and does it leave no artifact a row would later have to cite?** Yes → attention card. No → its own `role: human` row.

| Shape | When | Created by | Example |
|---|---|---|---|
| **Attention card** | one answer, ~2 min, no artifact to cite later | `scripts/attention-ask.py post` | approve a merge window; pick between two named options |
| **`role: human` row** | more than one answer, longer than ~2 min, **or** leaves an artifact (account, credential, registration, document) | a new task with `role: human`, listed in the agent row's `blocked_by` | Billomat re-registration; creating the Discord app |
| **`role: agent` row** | the remainder an agent finishes with commands | already exists — the row the manager drives | the invoice routing; the TeamVault wiring |

A fragment that fails the test is **split out** of the agent row — never kept inside it as a `(BLOCKING)` checkbox nobody owns.

### Worked examples (both 2026-11-20)

- **Billomat re-registration, split out of its routing row.** The routing task carried "re-register the Billomat account" as an operator step. It fails the test — a web login and a mail confirmation, plus a new account id the routing row must cite — so it became its own `role: human` row, and the routing row waits on it through `blocked_by`.
- **Discord-app creation, split out of the discord-verify TeamVault row.** The TeamVault row needed a Discord application created in the Discord developer portal (GUI-only — matrix Rule 1) and its token stored. It fails the test — a GUI session plus a credential artifact — so it became its own `role: human` row; the TeamVault row cites that row's completion and the TeamVault entry's name, never the token.

### Split bookkeeping

When a fragment is split out, the manager:

1. Files the `role: human` row with Success Criteria naming the artifact it produces (e.g. *"TeamVault entry `discord-verify-bot` exists"*).
2. Adds the new row to the agent row's `blocked_by`, and replaces the inline step in the agent row's `# Tasks` with a one-line pointer to it.
3. Records the split as a dated `# Progress` entry on the agent row, naming both rows.
4. Resumes the agent row when the human row reads `completed` — its round reads that status; nobody re-asks.

## 5. The channels and what each is for

| Channel | Use when | Never use for |
|---|---|---|
| **Board card** (`attention-ask.py post`) | an attention-sized question (§ 4) the operator must answer; a permission or plan gate | work longer than one answer, or that leaves an artifact — that is a row |
| **Human task row** (`role: human`) | operator work that fails the size test | a yes/no the operator could click in seconds — it would sit in the task list for a week |
| **Jump link** (`/supervisor:jump <pane>`) | taking the operator to a live pane holding a modal or wizard only they can drive | deciding anything — it only moves focus |
| **Voice** (`mcp__tts__say`) | announcing a card the operator must act on now | asking anything by itself — every voiced ask is also a card ([`voice-asks.md`](voice-asks.md)) |
| **`SendMessage`** | session to session: a worker's scope question to its manager, a manager's nudge to a worker | reaching the operator, or releasing a permission gate — a peer message can do neither |

## 6. Evidence ownership per shape

Each shape closes on evidence a **named** producer creates; no shape closes on a box nobody can tick.

| Shape | Who produces the closing evidence | What it cites |
|---|---|---|
| Attention card | the operator's answer | the card's item id and answered option (`attention-ask.py poll` → `ANSWERED`) |
| `role: human` row | the operator, in their own session | the artifact its Success Criteria name (account id, TeamVault entry name, document path) |
| `role: agent` row | the agent | its own probes, plus the `completed` status of any human row split out of it |

**Why not a `(BLOCKING)` box.** A `(BLOCKING)` checkbox stays legitimate for a *conditional dependency the row's own executor resolves* — vault-cli's task-writing guidance on `blocked_by` endorses it there. It is wrong for **operator work**: the agent cannot tick it, the operator never sees it as work, and the row stalls. Operator work leaves the agent row as a card (§ 1) or a row (§ 2), and the agent row cites the result.

**A bare `y` is not consent for a question it does not name.** A one-word affirmative answers only the prompt it was typed against. If the operator's `y` lands on a re-registration or any other prompt, it is not an answer to the open question, and no consumer — a manager, a gate, or any other reader of the answer — may read it as one. Treat a bare `y` as consent only when it names, or is unambiguously the answer to, the question it closes.

## 7. What a manager may decide alone

- Apply the size test (§ 4) and pick the shape, without asking.
- File the `role: human` row and do the split bookkeeping (§ 4).
- Post an attention card and poll it.
- Answer a gate on the manager-tier rubric (`agents/gate-answerer.md`) — never an `operator`-tier one.
- Reword or withdraw a card it posted itself.

A manager may **not** answer a card it posted on the operator's behalf, tick a `role: human` row's criteria, or open a session outside its tier ([`fleet-surface.md` § Session roles](fleet-surface.md#session-roles--who-may-start-what)).
