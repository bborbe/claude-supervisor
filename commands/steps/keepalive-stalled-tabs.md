---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps/<name>.
user-invocable: false
---

# Keep alive — stalled tab workers

When: every tick — the eighth procedure in `commands/steps/keepalive.md`, reached from
`agents/fleet-keepalive-actor.md` § 4's *four worker mechanisms* note. ⚠️ **It is dispatched
from there and nowhere else**; a steps file no dispatch names is a procedure that never runs,
and the whole feature stays inert while reading as built.

The sweep already classifies a tab worker `stalled` (`agents/fleet-sweep-reader.md:115`:
`busy`/`shell`, unchanged task mtime across ≥2 sweeps, `--ages` reading `quiet`). Nothing acted
on it, because the act — `scripts/restart-worker.py` — refuses `busy-target`, and a stalled
session is `busy`/`shell` **by definition**. The only lift is `--allow-busy-lapse`, and the
operator ruled on 2026-11-12 that an unattended round may not pass it.

So this leg **reports and hands over**; it never restarts on its own judgement.
`scripts/stalled-tab-restart.py` owns both halves and every guard.

## Invoke it — one `python3` call, never a shell `for`

```bash
P=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts
F=$(python3 "$P/resolve-folders.py" --vault "<vault>" --require tasks_dir)
python3 -c '
import json, subprocess, sys
vault, f, script = sys.argv[1:4]
if f.startswith("UNKNOWN"): sys.exit("folders: " + f)
# ⚠️ Field 2, not 0. `resolve-folders.py` prints `topics|goals|tasks|vault-name`, so
# `[0]` is the TOPICS directory and the leg would look for task pages among topic pages.
tasks = f.split("|")[2]
rows = json.load(sys.stdin)          # this round'"'"'s sweep rows
for row in rows:
    if row.get("class") != "stalled" or row.get("mode") != "interactive":
        continue
    out = subprocess.run(
        [sys.executable or "python3", script, "--vault", vault, "--task", row["task"],
         "--dir", tasks, "--session-id", row["session_id"],
         "--stall-count", str(row.get("stall_count", 0))],
        stdout=subprocess.PIPE, text=True).stdout
    lines = out.split("\n")
    print(row["session_id"][:8] + "\t" + "\n".join(lines[:2]).replace("\n", "\t"), end="\n")
' "<vault>" "$F" "$P/stalled-tab-restart.py" < <this round's sweep JSON>
```

⚠️ **One `python3` call, never a shell `for`.** `allowed-tools` grants fixed command
**prefixes**, and a `for … do … done` matches none of them, so a shell loop escalates a
permission prompt every round and the leg stops running unattended.

⚠️ **The population is `class: stalled` AND `mode: interactive`.** The headless half is
`scripts/stuck-heal.py`'s ladder and the fleet round's headless restart — a different
population with a different mechanism. `restart-worker.py` refuses `headless-target` itself,
so a mis-sent headless row is reported rather than acted on, but do not send one.

## Act on the verdict — the script's first line

| Verdict | What it means | What you do |
|---|---|---|
| `restart` | The operator answered this session's card; the real restart ran | Report the `✅ killed pid …` line. The fresh session's id is **NEW** — never quote the old one as resumed |
| `carded` | The card is posted, or already open and unanswered | Nothing. The row is the operator's now, and a second card would be the duplicate the dedup key exists to prevent |
| `capped` | Two restarts with no progress; one escalation card posted | Nothing further this round, and never a third restart |
| `held` | An operator hold — outranks every other signal | Nothing |
| `skipped` | Another refusal token; the **second line is the token itself** | Report it verbatim and leave the row for the next round |
| `cannot-tell` | A probe could not decide | Report it and never act |

⚠️ **This leg never passes `--allow-busy-lapse`.** The script's answered path is the only call
site that does, and it is reached only from the operator's `ANSWERED:` on the card. A leg that
ran the script with the lift itself would be the unattended restart the ruling forbids, and it
would read exactly like a correct round.

⚠️ **`--dry-run` on the unattended path is a control, not caution.** Without it the script
proceeds on any row that reads `idle` at call time, so the forbidden unattended restart becomes
reachable through the window between the sweep and the call. Do not "simplify" it back.

## Report the reading — in the leg's output, and say it is not yet a digest key

Print one line per stalled row (session id8, verdict, and the token for `skipped`), then a
summary:

```
stalled-tabs: <n> seen · <restart> restarted · <carded> carded · <capped> capped · <skipped> skipped
```

⚠️ **`fleet-legs.py`'s `LEGS` section is a separate contract.** Its `KEYS` list, the sweep's
seed, `check-handback-keys.py` and six `test_fleet_legs.py` fixtures all encode the four-key
shape, so a new key is its own change with its own tests. Until that lands this reading lives
in the leg's output, and it must **not** be written into the digest under another leg's key: a
count recorded against `dead-workers` would be a measurement the digest never took, which is
the failure `commands/steps/output-shape.md` item 14 exists to prevent.

## Why there is no card to extend

`CLAUDE.md:135` states it: *"A stall is a duration, not an event, and it issues no attention
card."* `manager-attention-watch.py` renders `stuck-tab:` as a gate **reason** plus an
incident-ledger row, and its only `attention-ask.py post` path is the park card
(`--dedup-key park:<sid8>`). This leg **creates** the card, keyed `stuck-tab:<slug>`, so the
gate reason and the card cannot both accumulate for one session.
