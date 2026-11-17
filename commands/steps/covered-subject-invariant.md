---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Covered-subject invariant

When: a covered subject's manager is live

    - ⚠️ **Manager-first is a ROUTING rule, never a trust rule — the fleet still reads the board itself.** Dropping an entry because its manager is live is correct only while that manager is actually raising the ask. A covered subject whose non-terminal rows are **all blocked on the operator** and which has **no open card attributable to it** is a subject the whole stack has gone quiet on, and the manager's own report cannot rule that out: a manager that has stopped posting reports the same `waiting-on-human` rows either way. Measured 2026-10-08: `MDM Bugs` had a live manager, so this bullet raised nothing for it; that manager's under-target branch then suppressed its own card; the operator was told he was being waited on with nothing on the board to answer. **So run the model-free invariant over every covered subject whose manager is live.** ⚠️ **The script is VAULT-side, not plugin-shipped:** `sweep-gate.py` lives in the **active vault's** `.claude/scripts/`, is delivered by that vault's own git, and **this repo does not carry it** — `--invariant` appears nowhere in this tree. The plugin and the vault therefore ship this rule **together** and neither alone is sufficient: this bullet without the vault's script names a flag that resolves to nothing, and the script without this bullet has nothing reading its verdict.

      ```bash
      python3 '<vault>/.claude/scripts/sweep-gate.py' --topic '<subject>' --vault '<vault>' --invariant
      ```

      ⚠️ **Single-quoted, not double** — a subject name is vault-authored model-written content and can carry `"`, a backtick or `$(`, which inside double quotes command-substitute **before** `python3` sees the argument. This is the repo's own rule for exactly this class of value (`commands/goal-loop.md`'s row-claim bullet); the other interpolations in this file are double-quoted because they are not vault-authored.

    ⚠️ **The card is NOT this path's to post — the cron poster owns the ask.** The open-card card is raised model-free by the attention-controller `sweep-gate-notify-job`, which runs `sweep-gate.py --topic … --tick` for every subject with a live manager and posts the side-file card as producer `sweep-gate` / kind `cron`. **Two posters on one dedup key do not collapse:** the store's suppression is a point read of `(producer_id, dedup_key)`, so a manager-posted twin with the same `open-card-invariant:<slug>` key sits BESIDE the cron card as a second open row for one ask — the operator sees the same question twice, with two different producers behind it. Measured 2026-11-16: 21 open `open-card-invariant:*` items, **all `sweep-gate` / `cron`, none `manager`**. So **this step posts nothing and claims nothing on the asked-of-you ledger.** The dedup key has one minting home (`invariant_card()` in that script); this file names neither its format nor a claim on it.

    **The probe stays, as a DIAGNOSTIC — a payload means the cron poster missed a subject, not that the fleet should card it.** It prints a card payload on stdout **exactly** when the subject is operator-blocked with zero cards of its own, and nothing otherwise — an empty stdout is the ordinary case, not a failure. A payload is therefore a *miss*: the subject is blocked on the operator, the board carries nothing for it, and the model-free poster has not reached it. **Report it ONCE PER ROUND in one line** — `open-card invariant: <subject> operator-blocked with no card — cron poster missed it` — and post nothing. ⚠️ **The EXIT CODE, not stdout, is what separates a quiet subject from a broken install — and `3` is neither.** `0` → read stdout as above; **`3` → another layer already holds this ask, so drop it and say nothing** (that is the script working, not failing); **any other exit — including `python3`'s own `2` when the script is absent at a typo'd or not-yet-landed `<vault>` path — means the vault-side script is unavailable: report it ONCE PER ROUND in one line, not once per subject, and post nothing.** A missing script and a genuinely quiet subject both produce empty stdout, and only the exit code tells them apart; without this arm a broken install degrades to silence, which is the exact failure this bullet exists to remove. ⚠️ **`0` and `3` are different contracts and must not be merged** — collapsing `3` into "unavailable" reports a working script as broken once per round for every subject another layer legitimately holds.

    ⚠️ **A stale cron poster is its own line, read from its own record.** The poster writes `last-pass.json` at the end of every pass; its absence or age is the only thing that says the asker has stopped. Read it once per round — directory from `SWEEP_GATE_LOOP_STATE_DIR` when set, else `~/.claude/state/sweep-gate-loop`:

      ```bash
      python3 - "$SWEEP_GATE_LOOP_STATE_DIR" <<'PY'
      import json, os, sys, time
      d = sys.argv[1] or os.path.expanduser("~/.claude/state/sweep-gate-loop")
      p = os.path.join(d, "last-pass.json")
      try:
          with open(p, encoding="utf-8") as handle:
              record = json.load(handle)
      except FileNotFoundError:
          print("STALE no last-pass.json in %s" % d); raise SystemExit
      except (OSError, ValueError) as exc:
          print("MALFORMED %s (%s)" % (p, exc.__class__.__name__)); raise SystemExit
      if not isinstance(record, dict):
          print("MALFORMED %s (not an object)" % p); raise SystemExit
      try:
          end_epoch = int(record["end_epoch"])
          duration_s = int(record.get("duration_s") or 0)
      except (KeyError, TypeError, ValueError):
          print("MALFORMED %s (end_epoch/duration_s not integers)" % p); raise SystemExit
      age = int(time.time()) - end_epoch
      bar = max(3 * duration_s, 1800)
      print("%s age=%ds bar=%ds last=%s" % (
          "STALE" if age > bar else "OK", age, bar, record.get("started_at_iso", "?")))
      PY
      ```

      ⚠️ **The bar is derived from the record, never hardcoded.** `3 × duration_s` — a measured pass runs 767–1347 s, so a bare 1800 s bar would fire during one ordinary long pass, while 3 × duration_s means two full cycles must be missed. The 1800 s floor is the same basis `~/.claude/scripts/sweep-gate-notify-stale.sh` uses for **heartbeat** age (`limit=$(( 2 * INTERVAL ))`), reused as a floor only — that check answers a different question and its own header says the 2× basis is wrong for a pass. **Three verdicts, three reports.** `STALE` → report ONE line — `sweep-gate poster STALE — <last>, <age>s old (bar <bar>s)` — so a dead asker is visible instead of silent. `MALFORMED` → report ONE line naming the path and the fault — the record exists but cannot be read, so staleness cannot be judged, and silence would hide exactly the broken state this check exists to surface. `OK` → say nothing.

    ⚠️ **What this path must NOT do, and why the old instruction was removed.** Until 2026-11-16 this step claimed the subject on the asked-of-you ledger (`--row`) and posted its own card whenever the probe printed a payload. That made the fleet a second asker for an ask the cron poster already owns, and it starved in the ordinary case anyway: `invariant_card()` returns `None` before it can produce a payload while any card is attributed to the subject, so the fleet's own card and its claim never existed while the cron card was open. The ledger could not arbitrate it either — claims resolve only when an asker relays an answer, and nothing relays a board card (17 open claims, oldest 52 days). One declared asker plus a stale-poster line replaces a second asker that never fired.
