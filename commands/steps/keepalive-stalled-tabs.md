---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps/<name>.
user-invocable: false
---

# Keep alive — stalled tab workers

When: every tick — the eighth procedure in `commands/steps/keepalive.md`, reached from `agents/fleet-keepalive-actor.md` § 4's *four worker mechanisms* note. ⚠️ **Dispatched from there and nowhere else:** a steps file no dispatch names never runs. The sweep classifies a tab worker `stalled` (`agents/fleet-sweep-reader.md:115`), but `scripts/restart-worker.py` refuses `busy-target` and a stalled session is `busy`/`shell` **by definition**. The only lift is `--allow-busy-lapse`, which the operator ruled (2026-11-12) stays their own judgement — so this leg **reports and hands over**, and `scripts/stalled-tab-restart.py` owns every guard.

## Invoke it — one `python3` call, never a shell `for`

```bash
P=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts
F=$(python3 "$P/resolve-folders.py" --vault "<vault>" --require tasks_dir)
python3 - "<vault>" "$F" "$P/stalled-tab-restart.py" <<'PY'
import json, os, subprocess, sys
vault, f, script = sys.argv[1:4]
if f.startswith("UNKNOWN"): sys.exit("folders: " + f)
tasks = f.split("|")[2]        # prints topics|goals|tasks|name — [0] is TOPICS
snap = os.environ.get("FLEET_SNAPSHOT_PATH") or os.path.expanduser(
    "~/.claude/state/fleet-snapshot.json")
try:                           # absent after fleet-reset.py, and on a first round
    with open(snap, encoding="utf-8") as fh:
        sessions = (json.load(fh) or {}).get("sessions") or {}
except (OSError, ValueError) as error:
    print("stalled-tabs: cannot-tell — " + str(error)); sys.exit(0)
for sid, row in sorted(sessions.items()):
    row = row if isinstance(row, dict) else {}
    try: count = int(row.get("stall_count") or 0)
    except (TypeError, ValueError): continue
    tf = row.get("task_file") or ""
    if count < 2 or not tf: continue
    try:
        out = subprocess.run(
            [sys.executable or "python3", script, "--vault", vault,
             "--task", os.path.splitext(os.path.basename(tf))[0],
             "--dir", os.path.dirname(tf),   # the PAGE's dir: goals tier, not just tasks
             "--session-id", sid, "--stall-count", str(count)],
            stdout=subprocess.PIPE, text=True, timeout=420).stdout  # > the script's 390s
    except Exception as error:    # one bad row must not abort the round
        print(sid[:8] + "\tcannot-tell\t" + type(error).__name__); continue
    print(sid[:8] + "\t" + "\t".join(l for l in out.split("\n") if l.strip()))
PY
```

⚠️ **A heredoc, not `python3 -c '…'`** — the comments hold apostrophes, which terminate a single-quoted `-c` string: the invocation died with a bash syntax error before the first row. ⚠️ **The snapshot, not a sweep payload** — no producer emits rows with `class`/`mode`, so `stall_count >= 2` is the whole population test, and `restart-worker.py` refuses `headless-target` itself. ⚠️ **That counter is not the whole `stalled` verdict** (the reader also needs `--ages` reading `quiet`, which the snapshot lacks), so this population is a **superset** — safe, because the act is a card, not a restart. ⚠️ **One `python3` call, never a shell `for`** — `allowed-tools` grants fixed command prefixes, so a loop escalates a prompt every round.

## Act on the verdict — the script's first line

| Verdict | Meaning | What you do |
|---|---|---|
| `restart` | The operator answered; the real restart ran | Report the `✅ killed pid …` line. The new session's id is **NEW** |
| `carded` | Posted, or open and unanswered | Nothing — a second card is the duplicate the dedup key prevents |
| `capped` | Two restarts, no progress; one escalation posted | Nothing further, and never a third restart |
| `held` | An operator hold — outranks everything | Nothing |
| `skipped` | Another refusal; **line 2 is the token** | Report it verbatim, leave the row for next round |
| `cannot-tell` | A probe could not decide | Report it and never act |

⚠️ **This leg never passes `--allow-busy-lapse`** — the script's answered path is the only call site that does, reached only from the operator's `ANSWERED:`. ⚠️ **`--dry-run` on the unattended path is a control, not caution:** without it the script acts on any row reading `idle` at call time, making the forbidden unattended restart reachable through the sweep-to-call window.

## Report the reading — in the leg's output, not yet a digest key

One line per stalled row, then `stalled-tabs: <n> seen · <restart> restarted · <carded> carded · <capped> capped · <held> held · <skipped> skipped · <cannot-tell> cannot-tell`.

⚠️ **All six verdicts carry a column** — a round whose rows are all held or undecidable would otherwise report none of them. ⚠️ **`LEGS` is a separate contract** (`fleet-legs.py`'s `KEYS`, the sweep's seed, `check-handback-keys.py` and six `test_fleet_legs.py` fixtures encode the four-key shape), so this reading stays in the leg's output and must **not** go under another leg's key. ⚠️ **There is no card to extend** — `CLAUDE.md:135`: *"A stall is a duration, not an event, and it issues no attention card."* So this leg **creates** it, keyed `stuck-tab:<slug>:<restarts>`.
