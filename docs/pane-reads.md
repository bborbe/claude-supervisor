# Transport reads

The rule every read of an external transport in `scripts/` shares: `wezterm cli list`, and every `ps` invocation. A read of that kind is a *question with three possible answers*, and a script that can only express two of them will report a broken transport as a quiet fleet.

**Why this is one file now.** The same defect was repaired four times in two evenings, each pass finding more sites than the task that prompted it — and each fix was decided per site with nothing written down, so the next pair regressed. Three surveys on 2026-09-25/26 each surfaced more instances than they were scoped for. Written out 2026-09-26, when the audit that enumerated the whole class found seven sites and no shared statement of what they had in common.

## The rule

**A failed transport read must never be representable as an empty result.**

An empty result is a real answer: the transport replied, and the answer was "nothing". A failed read is the *absence* of an answer. Collapsing the second into the first is the defect — and it is silent, because every downstream consumer is built to handle an empty result correctly.

So the accessor's contract:

1. **Return `None` on failure.** Never `{}`, `[]`, or `0`. Those are reserved for a transport that answered with zero results. `None` is the third state, and it is the only thing that distinguishes "no panes exist" from "I could not ask".
2. **Never re-collapse it.** A caller must not write `x or {}`, `x or []`, or `if not x:`. Each of those discards the third state and re-creates the defect one level up. The wrapper that does this is the most dangerous site in the class, because it looks like a convenience.
3. **Decide the response from what the caller does with the value.** Not from the accessor, and not by copying a sibling — two callers of the same failed read legitimately respond differently.

## The three responses

| the caller… | response | precedent |
|---|---|---|
| only renders the value | **degrade loudly** — name the failure, continue | `context-usage.py:155` — `⚠️ \`wezterm cli list\` unreadable — pane ids withheld` |
| decides liveness or identity from it | **preserve `None`** and refuse to draw the conclusion | `jump-link.py:71` — returns `None`, docstring: *"a label is a nicety, not a gate"* |
| already exits non-zero on failure | **refuse non-zero**, naming the transport | `who-needs-me.py:1291` — `if pmap is None: … sys.exit(1)` |

## The four severities

A collapse is not one bug. Classify it before fixing it, because the fix differs:

- **positive false claim** — the site prints a count, an `N headless`, or a `"paned": 0` as fact, at exit 0. The worst kind: a wrong answer certified by the success code, so nothing downstream can tell it from a real one. Fix: stop asserting the number; name the failure instead.
- **silence** — the failure is swallowed and the caller reports a normal-looking empty result, asserting no wrong number. Fix: surface the failure, so the empty result stops being ambiguous.
- **attribution** — the failure *is* reported but not named as a transport failure, so a broken mux socket cannot be told from a genuinely empty fleet. Fix: name the transport in the message. The exit code is usually already right.
- **false refusal** — the wrong case is refused: a healthy-but-empty fleet is rejected as though the transport had failed. The mirror of the others, and the one a naive "always warn" fix produces. Fix: distinguish empty from failed before refusing.

⚠️ **A class, not a site list.** Binding a severity to a named exemplar was tried twice on this codebase and went wrong both times — the exemplars drift as the code moves, so the prose breaks while the classes stay right. Enumerate sites from current source; never inherit a line number from a document.

## Checking a new read

Ask of every new transport read, in order:

1. **Does it return `None` on failure?** If it returns `{}` / `[]` / `0`, it is a site — stop here.
2. **Does any caller re-collapse it** (`or {}`, `if not x:`)? That caller is a site, and a more dangerous one than the accessor.
3. **Is the caller's response the right one for what it does with the value** — degrade loudly, preserve `None`, or refuse non-zero?
4. **Does the healthy-but-empty case still read as empty?** A fix that cannot tell empty from failed has traded a false zero for a false refusal.

The walk in `scripts/transport-read-walk.py` enumerates the consumers mechanically — it starts from every `subprocess` call site and follows **callers** to closure, which is the direction that reaches a consumer holding no subprocess of its own. Its companion is a grep for the call shapes; the two are independent, and a read one reaches and the other misses is a disagreement worth resolving rather than averaging.
