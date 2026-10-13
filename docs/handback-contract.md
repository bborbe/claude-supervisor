# Hand-back contract

The shape a sweep's **read half** and **act half** return to their caller. Four agents carry it — `agents/manager-sweep-reader.md` and `agents/manager-drive.md` for the manager loop, `agents/fleet-sweep-reader.md` and `agents/fleet-drive.md` for the fleet loop — and each carries **a pointer to this file** rather than its own copy of the rule. What a leg computes stays in its own agent file; only the shape of what it hands back is here.

**Why one file.** Four agents returning four shapes is a liability the caller cannot see. The caller renders one table from whichever leg answered, so a leg that returns prose where verdict lines were expected degrades the round **silently** — the table still renders, nothing errors, and the missing verdict reads as *nothing to report*. The size cap is here for the same reason the shape is: a leg that hands back its whole corpus charges the caller's context for it on every round, and that recurring cost is what this contract exists to remove. The rule was carried verbatim by each agent before this file existed, and a rule carried by four copies drifts — one copy is amended and the others silently keep the old behaviour.

## The shape

- **Verdict lines and counts, in `key=value` form.** One line per verdict: the key names the verdict, the value carries its count or its short state. The key names are each leg's own and are defined in that leg's agent file — this contract fixes the **form**, never the vocabulary.
- **≤ 1 KB**, measured as the byte length of the text the leg returns.
- **Non-empty, and carrying that leg's required set.** An empty hand-back is trivially ≤ 1 KB and satisfies nothing here. The required set is the verdict the caller cannot reconstruct for itself:
  - the **sweep reader** returns the **bucket counts** — every bucket name the runbook declares, with its count, including the ones at zero;
  - the **drive leg** returns the **drive verdicts** — one per row it decided, with the decision.
- **The full payload never rides the hand-back.** A leg that rendered a table, classified a set or drafted a batch writes it to disk and hands back the verdict about it. Inlining the payload is the failure this half of the contract removes: it is the same information at many times the size, and it arrives in prose where nothing downstream can check it.

## Where the full payload goes

The paths are siblings under one directory, keyed by the subject slug — the subject lowercased with non-alphanumerics collapsed to `-`. They are separate files rather than fields of one record because they hold different things and can legitimately disagree; that disagreement is the point.

| Written by | Path | Holds |
|---|---|---|
| `manager-predispatch.py --write-payload` | `~/.claude/state/manager-predispatch/<slug>.table` | the table the sweep reader rendered, byte-for-byte, carrying the render's own mtime |
| `manager-predispatch.py --save` | `~/.claude/state/manager-predispatch/<slug>.json` | the gate's verdict — digest, table, `recorded_at` |
| the caller | `~/.claude/state/manager-predispatch/<slug>.tracked.txt` | the tracked set the caller resolved |
| the caller, from the drive leg's `Audit` block | `~/.claude/state/manager-predispatch/<slug>.verdicts.json` | the drive leg's per-row audit, cached so the next round does not re-audit cold |

⚠️ **The store is the producer, not the sweep-gate snapshot.** `~/.claude/state/sweep-gate-loop/<vault>/<subject>.snapshot.json` is a different file on a different cadence, and it carries no bucket concept — a per-bucket set is never obtainable from it. The rule and its measurement have one home: `docs/fleet-surface.md` § Session end.

## What this file does not own

Each leg's checks, classifications, thresholds and render stay in its own agent file, and its caller's dispatch contract stays in the calling command. In particular this file does **not** state which verdicts a leg must produce beyond the required set above, how a leg measures its own output, or when a caller reads a payload back — those are the leg's and the caller's own rules, and restating them here would give each a second home.
