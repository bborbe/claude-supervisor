# Session end record

One tab-separated line per event, per pane, at `~/.claude/state/session-exit/pane-<pane>.log`
(`DEFAULT_DIR` in `scripts/session-exit-record.py`). This is the single home for **how a local
session ended**; the spawn shape that writes it stays in
[`docs/fleet-surface.md`](fleet-surface.md) § Spawn a worker, and the kill-and-resume procedure
in [`docs/restart-worker.md`](restart-worker.md).

## Why it exists

Every other surface that names a session — the task file's `claude_session_id`, the session
registry at `~/.claude/sessions/<pid>.json` — records that a session **started**. Nothing
recorded that it **ended**, when, or how. A pane killed with `kill -9` and a pane that was
never spawned were indistinguishable from every surface the fleet reads, so a death could not
be attributed after the fact.

## The line

```
2026-10-27T10:36:07Z	kind=signalled kill	pane=2201	session=-	pid=80188	rc=137	signal=SIGKILL	sender=unavailable(eslogger rc=1 ES_NEW_CLIENT_RESULT_ERR_NOT_PRIVILEGED)	writer=trap
```

Nine fields, always in this order: `ts · kind · pane · session · pid · rc · signal · sender ·
writer`. An unknown value is `-`, never an empty field.

⚠️ **The record is keyed on the pane, not the session id.** The pane is the process container
that dies; a resumed session is a new process in a new pane, and two panes are two ends that
must not overwrite each other. `session=-` is a normal state for a pane whose session id was
never handed to the wrapper.

## The kinds

| `kind` | Written by | Means |
|---|---|---|
| `spawn` | trap | the wrapper started; a **start** marker, never an end |
| `heartbeat` | observer | the watched pid was still alive at this tick (every 30 s) |
| `exited` | trap | the child exited on its own; `rc` is its status |
| `signalled kill` | trap | the child was signalled; `rc` is `128+n` and `signal` names it |
| `unknown-end` | observer | the watched pid disappeared — an **inference**, see below |
| `closed-tab` | — | in the vocabulary, **never emitted** |

⚠️ **`rc` alone does not classify.** `rc=1` and `rc=128` are exits, not signals; only `rc>128`
is read as `128+n` and mapped through `SIGNALS` (`1 SIGHUP`, `2 SIGINT`, `3 SIGQUIT`,
`9 SIGKILL`, `15 SIGTERM`). An unnamed signal prints its number rather than being guessed at.

⚠️ **`closed-tab` is never written.** WezTerm exposes no pane- or tab-close event — 15 events
were enumerated on 2026-10-27, 13 window and 2 mux — and `SIGHUP` is not specific to a tab
close. The kind is in the vocabulary so that a future writer can emit it without changing the
format; nothing today does.

## Two writers, layered by what each survives

- **The pane shell's `EXIT` trap** (`writer=trap`) — covers a normal exit, a non-zero exit, and
  a signal delivered to the *child*, because the shell's `waitpid` reports `128+n`. It cannot
  cover a kill that takes the shell itself.
- **A detached observer** (`writer=observer`) — double-forks, `os.setsid()`s into its own
  session and process group, and reports through a pipe before the wrapper proceeds, so
  `kill -9 -<pane-pgid>` cannot reach it. It polls the pane shell's pid, writes a heartbeat per
  tick, and on the pid's disappearance writes `unknown-end`.

⚠️ **`unknown-end` is an inference and never claims `signalled kill`.** A signal is a fact only
the parent's `waitpid` can report, so a pane whose shell vanished is recorded as *the pid
disappeared* and nothing more. It re-reads the log before writing and stays silent when an end
line is already present, so the trap's report always wins over the observer's.

⚠️ **`exec` is what made this impossible before.** `exec "<launcher>"` replaces the pane's shell
with the launcher, so no process is left to `waitpid` and the exit status stops existing — `137`
is a value only a *parent* can read. Every spawn call site installs the trap instead; see
`docs/fleet-surface.md` § Spawn a worker for the shape.

## Reading it

```bash
scripts/session-exit-record.py read --pane 2201     # one pane's lines
scripts/session-exit-record.py list                 # every pane, path + line count
scripts/session-exit-record.py list --json          # records: [] on a missing store
scripts/session-exit-record.py path --pane 2201     # the file it would write
```

`--pane` defaults to `$WEZTERM_PANE`, so a command run inside the pane needs no argument.

## What it cannot know

`sender` names who ended the session, and the only mechanism that could answer is `eslogger`,
which is root-only. Measured on 2026-10-27: `/usr/bin/eslogger exec` exits 1 with
`Not privileged to create an ES client, need to be superuser
(ES_NEW_CLIENT_RESULT_ERR_NOT_PRIVILEGED)`. That refusal is **quoted into the record** rather
than asserted from documentation, so a reader sees what was tried. A normal exit records
`sender=none` — there is no sender to name.

⚠️ **This is not a liveness source.** It answers "did this pane end, and how" after the fact; it
does not answer "is this session alive now". The registry entry, which is deleted when the
session exits, is what makes an absence mean something — see [README](../README.md) § Adopting
an existing session.
A pane whose record shows only `spawn` and `heartbeat` is alive or was killed inside the last
tick; one with an end line is finished, and the line says how.
