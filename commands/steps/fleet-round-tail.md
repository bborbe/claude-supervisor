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

It prints `INCOMPLETE ROUND — skipped: <step> — <why>` for every field that does not resolve to an artifact produced **this round**, and exits **11**; a full round prints nothing and exits **0**.

⚠️ **The line is positive on purpose.** The rounds measured on 2026-10-31 that skipped their digest had *already* dropped `Snapshot written` from their render, so an absence-based signal would have passed on exactly the rounds this exists to catch. Only a string that cannot exist at the pre-change revision discriminates — which is why the sentinel comes from the script rather than from a sentence a model is asked to remember.

## The debt — what makes the verdict outlive its round

The sentinel reports on the round that printed it, and it dies with that round: `--start` clears `fields`, so one round later nothing records that a step was skipped. Measured 2026-10-31: **five of twelve** post-deploy rounds armed and never wrote a digest. One printed the line (11:50); four never ran this check at all (10:42, 11:00, 13:11, 13:27); and the 13:27 round rendered a whole-round closer ending `⏰ Next: the 13:43 fleet round` with no digest behind it. The evidence was gone by the next round in every case.

So `--check` also writes the skipped steps to `<slug>.debt` beside the receipt, and **`--start` deliberately does not touch that file**:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manager-receipt.py" --debt --subject "Fleet Manager" --branch fleet
```

It prints `debt: <step>` per open step and exits **12** while any step is owed, **0** when clear. ⚠️ **It also exits 2, and that is a third state — not a clear one.** `--debt` returns 2 when it cannot read the debt file or cannot write the verdict (`debt()`, `scripts/manager-receipt.py`), and it prints **nothing to stdout** in that case, only a reason to stderr. The `--debt && <leg>` chain fail-closes either way, but the render must not: see `commands/steps/output-shape.md` item 13, which branches on the exit code rather than on empty output for exactly this reason. The three non-sweep legs chain it — `--debt && <leg>` in `commands/steps/drive.md`, `commands/steps/keepalive.md` and `commands/steps/flagged-open.md` — so a round that owes a sweep cannot spend itself on the cheap legs instead. The debt is rewritten on every evaluation, so a round that does its work pays it simply by passing.

⚠️ **`--debt` RE-EVALUATES the round in progress; it is not a read of the file alone.** This is load-bearing and it was a defect in the first cut. The gate sits at router rows 5 / 5b / 5c and the tail's `--check` at row 19, so a file-only read judges every round on the *previous* round's verdict: a round that had already swept would still be blocked by a debt it had paid, and the round that actually skipped would never be blocked at all — the exact inversion of what the gate is for. `--debt` therefore recomputes the skip set from the current receipt and the `.arms` ring (the same `_skips()` `--check` uses), rewrites the file with it, and only falls back to reading the file when the round cannot be judged at all — no receipt, or its window lapsed. Two consequences worth naming: a round that skips is blocked **on its own receipt**, which is the prevention; and a round that runs its legs but never its tail still records its own skip, because the gate is a writer too.

⚠️ **Exit 12 means "skip this leg", never "end the round".** A round that stopped at a gate would not wedge anything — `--debt` and this check both rewrite the debt, so the next round that does its work pays it by passing. What it would lose is the **render**: the tail's `--check` is what turns the skip into a line the operator reads, so a round that ends early leaves a skip nothing reported. That is the whole failure this check exists to end, one step short of ending it.

`--abandon --step <step>` is the single recovery verb: it removes one step from the debt, records it timestamped in `<slug>.abandoned`. `check()` honours a payment only inside the round's own window, bounded at both ends, so neither an old abandonment nor a clock-jumped one can excuse the step — and prints `abandoned <slug> <step>`. Use it when the round genuinely will not do that step.

⚠️ **Never block the arm instead.** `fleet-keeper.py` (launchd, `StartInterval` 60 s) reads the digest ledger and resumes a round it sees as lapsed, so a refused arm becomes a crash loop rather than a recovery. The guard sits where the round *reads*, not where it arms.

⚠️ **`--branch fleet` is load-bearing, not decoration.** The fleet round writes its liveness under `fleet--fleet-manager` (the slug `manager-liveness.py` builds from `--branch fleet`); a receipt keyed on the bare `fleet-manager` reads a file the arm step never wrote, so every round would report `skipped: re-arm` whether or not it armed. The same flag is what makes `--start` / `--record` write the receipt this check reads — a round that records under one slug and checks under another reports `skipped: receipt`.

⚠️ **The fleet tier owes two steps, not the goal tier's four:** `re-arm` (an arm epoch **inside** the round window, read from the `.arms` ring — the file's existence proves nothing, since a ring survives from earlier rounds and `manager-liveness.py:345` empties it at the first arm after a stand-down) and `digest` (the sweep reader's rendered payload, identified by its own `DIGEST … sessions … snapshot written:` opening line). A missing digest is reported as `skipped: digest`, never folded into `skipped: sweep`.

⚠️ **Surface every `skipped:` line in this tick's render, and re-arm regardless of the verdict.** An exit code nobody reads is the failure this check exists to end: a round that printed `skipped: digest` into a Bash result and then rendered a complete-looking table has replaced one silence with another. The tail runs **before** the output-shape row for exactly that reason — a skip discovered after the render cannot reach the block the operator reads.

⚠️ **Re-arm regardless.** The verdict is a report, not a brake: a round that skips a step still arms the next one, so the fleet is not left unwatched by the very check that found the gap.

⚠️ **Run it at the tail, against the round that just ran.** The check reads the receipt `--start` opened this round, so running it before the round's own steps have run reports the round as empty — correctly, and unhelpfully.

⚠️ **What this check catches, stated exactly — because a check that over-claims is worse than one that says where it stops.**

- **Catches: a round that opened its receipt and skipped a step.** The arm missing → `skipped: re-arm`; the digest missing → `skipped: digest`. That is the measured 2026-10-30 shape and the reason this row exists.
- **Catches: a round whose receipt is older than its own window** — reported as `skipped: receipt`, because `--check` refuses a round that started more than `observed_period() + SLACK` ago (`period + 180`, or the declared interval plus the same `SLACK` before four arms exist).
- **Catches, one round late: a skipped step that nothing read.** `<slug>.debt` is a file, not a render, so `--debt` fires on a round that never ran this check — the 13:11 and 13:27 shape — and on every round after it until the step is done or abandoned. It does not require this tail to have run, which is the property a printed line cannot have.
- **Does NOT catch: a round that skips a step and reaches neither this check nor a gated leg.** The debt is written by `check()` **and by `--debt`**, so a round that runs any of the three gated legs records its own skip even when it never reaches the tail. The call sites that write it are **six**: this check (row 19), the three gated legs (rows 5 / 5b / 5c), the `--debt` call inside `output-shape.md` item 13 (row 20), and `--abandon`. Item 13 runs on every tick, so a round that skips its sweep but still renders its output records its own skip there. A round that makes *none* of those six calls — no gated leg, no render, no tail, no abandon — records nothing of its own, and `--debt` fires on the round after it only when an *earlier* evaluation left the debt open. Read the two halves separately: the **read** never depended on the tail having run, and the **write** now needs only one of six calls rather than the one at the end. That residual is why the arm is never blocked and why `fleet-digest.py --check` still exists — the debt carries a skip that was seen, it does not manufacture one that nothing looked at.
- **Does NOT catch: a round that skipped `--start` itself.** The previous round's `round_started_at` and `fields` survive, the new arm and digest are recorded into that stale receipt, and both predicates resolve against the *previous* window — so one such round can read green. It is bounded: the stale window lapses within `period + 180` (≈18 min at a 15-minute cadence), so at most one round is masked before `skipped: receipt` starts firing. Closing it needs a witness outside the receipt — a per-round id compared against the `.arms` ring — and a wrong guess there produces a false red on a healthy round, which is the failure this whole check exists to prevent. Named here rather than fixed blind.
- **Does NOT catch: a round that never ran at all** — a hand-assembled substitute never reads this router table, so no tail row fires. That shape is `scripts/fleet-digest.py --check`'s job: it ages the newest digest independently of any round, and re-fires while the age keeps rising (`scripts/fleet-digest.py` § `--check`). Neither check substitutes for the other.
