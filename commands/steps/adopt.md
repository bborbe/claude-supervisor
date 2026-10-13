---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Adopt

When: `adopt-orphans.py` printed rows or exited non-zero — run: `python3 $P/adopt-orphans.py --self "$CLAUDE_CODE_SESSION_ID"`

## Step 3a — Adopt: give an exited manager's workers an owner

**Runs after the sweep and before the drive leg** — the section sits above Step 3b so document order and execution order agree.
```bash
python3 $P/adopt-orphans.py --self "$CLAUDE_CODE_SESSION_ID"
```
**Read its exit code.** A refusal writes only to stderr and prints no stdout rows, so a round that never checks the code falls through to `(none stranded)` and reports a clean round for a read that never happened. The file already names the check where it matters (`orphan-candidates.py … exit code checked`, § Steps 0–3).
It prints, per exited manager, `manager <sid8> exited` and `<n> workers adopted from exited manager <sid8>`, and records each adoption through `ownership-claim.py` — the claim store `gate-owner-filter.py` already reads.
⚠️ **The adopter is a same-subject live successor when exactly one exists, else THIS session.** The rule, its two fallbacks, and why ambiguity falls back rather than picking live in the script's own docstring — read them there, never restate them here. `--self` names the fallback adopter and defaults to `CLAUDE_CODE_SESSION_ID`, which Claude Code exports into every Bash this session runs — so an ordinary round adopts into this session, and the refusal fires only where that variable is genuinely absent.
⚠️ **A non-zero exit means NOTHING was adopted, and the REASON differs — so print the script's own stderr line, never a fixed phrase.** Three refusals return 1: a missing tick session id (returned before any store is read), an unreadable spawn ledger, and an unreadable registry or heartbeat store. **Only the last is a liveness-unknown reading.**

## Render

Render Step 3a's lines **verbatim**: one `manager <sid8> exited` and one `<n> workers adopted from exited manager <sid8>` per exited manager with stranded workers, plus the `already held by a live manager` line where one claimed first. ⚠️ **Render the `  adopted by <sid8> (<successor|fallback>)` line too** — the count above it is identical whether a same-subject successor took the workers or THIS session did. On a **non-zero** exit print the script's own stderr line **verbatim** — it distinguishes the three refusals, and only one of them is a liveness-unknown reading — never a fixed paraphrase. ⚠️ **On a ZERO exit, still print any `adopt-orphans: claim failed for <sid8> (exit N)` line** — a failed claim is written per worker to stderr while `main()` still returns 0. ⚠️ **Capture stderr as well as stdout, and render the census line on every zero exit** — `adopt-orphans: exited managers with live workers: N  adopted: N  held: N`. The census and the claim-failure lines are **stderr-only** while stdout carries just the per-manager rows. ⚠️ **The census line IS the no-rows summary — do not also print `(none stranded)`.**
