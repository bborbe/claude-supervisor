---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Fleet round tail

When: every tick

## The round's receipt — the mechanical check that the steps ran

`commands/fleet-drive.md` is a router map: it names the steps and the condition under which each is read, and nothing verifies that any of them ran. A round can therefore arm its wakeup, take the readings and render exactly like a full one, while the digest was never written and nothing on the round's own tail said so.

Measured 2026-10-30: the fleet round ran `manager-liveness.py --arm` twelve times across a window in which **no digest was written for 6h38m**, and no line of that round's output distinguished it from a complete one.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manager-receipt.py" --check --subject "Fleet Manager" --branch fleet
```

It prints `skipped: <step> — <why>` for every field that does not resolve to an artifact produced **this round**, and exits **11**; a full round prints nothing and exits **0**.

⚠️ **`--branch fleet` is load-bearing, not decoration.** The fleet round writes its liveness under `fleet--fleet-manager` (the slug `manager-liveness.py` builds from `--branch fleet`); a receipt keyed on the bare `fleet-manager` reads a file the arm step never wrote, so every round would report `skipped: re-arm` whether or not it armed. The same flag is what makes `--start` / `--record` write the receipt this check reads — a round that records under one slug and checks under another reports `skipped: receipt`.

⚠️ **The fleet tier owes two steps, not the goal tier's four:** `re-arm` (an arm epoch **inside** the round window, read from the `.arms` ring — the file's existence proves nothing, since a ring survives from earlier rounds and `manager-liveness.py:345` empties it at the first arm after a stand-down) and `digest` (the sweep reader's rendered payload, identified by its own `DIGEST … sessions … snapshot written:` opening line). A missing digest is reported as `skipped: digest`, never folded into `skipped: sweep`.

⚠️ **Run it at the tail, against the round that just ran.** The check reads the receipt `--start` opened this round, so running it before the round's own steps have run reports the round as empty — correctly, and unhelpfully.

⚠️ **This catches a round that ran and skipped a step. It cannot catch a round that never ran at all** — a hand-assembled substitute never reads this router table, so no tail row fires. That shape is `scripts/fleet-digest.py --check`'s job: it ages the newest digest independently of any round, and re-fires while the age keeps rising (`scripts/fleet-digest.py` § `--check`). Neither check substitutes for the other.
