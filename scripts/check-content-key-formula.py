#!/usr/bin/env python3
"""The drive leg's `content_key` derivation must stay stated, and must keep excluding
the fields the audit does not read.

`agents/manager-drive.md` clause (1) is the single home of the key's derivation. Until
2026-10-06 that home named **no algorithm** — it said only *"keyed to the row file's
content"* — so each leg finished the sentence itself, and the caches on disk carried
**three** truncation lengths (2 at 12 hex, 7 at 16, 3 at 64; measured 2026-10-06T11:35,
12 caches / 92 entries). A cache written at one length can never match another, so a
drifted subject re-audits every one of its rows.

This check asserts the clause states, and keeps stating:

  (a) the algorithm — `sha256`
  (b) the truncation — `16 hex`
  (c) the four excluded fields — `mode:`, `last_auto_resume`, `claude_session_id`,
      `metrics_sessions`
  (d) the cut is *the fields the audit does not read*, so `status:` / `phase:` stay IN
      the key — a `phase` move from `todo` to `planning` is what turns
      `✅ Ready for approval` into `🚀 Ready`, and excluding it would freeze the label

(c) and (d) are the load-bearing pair, and neither alone is enough. (c) without (d) is a
list a later reader extends to *everything a tick writes*, which strips `phase` and
silently freezes every approval; (d) without (c) is a principle with no fields attached,
which is the state this check was written to end.

**What this check does not prove.** It is a prose-shape check over markdown, because the
derivation is executed by an agent — there is no function to call and no return value to
assert on. So it cannot prove the leg *computes* the key this way; it proves the clause
cannot silently lose the formula, which is the half a text scan can hold. The behavioural
half is the live-tick measurement (a row prepared on tick N reporting a cache hit on tick
N+1), which no grep can fake.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOME = "agents/manager-drive.md"

#: The clause's own opening words. Anchored on text rather than a line number: this file's
#: clauses have moved by tens of lines inside a week, and a line-anchored check fails for
#: a reason unrelated to what it guards.
CLAUSE_OPEN = "**The cache — re-audit only what changed.**"

#: (a) and (b) — the algorithm and the truncation, stated together. A formula without a
#: truncation is a formula each leg finishes itself, which is the drift this guards.
ALGORITHM = re.compile(r"sha256.*?16 hex", re.S)

#: (c) — every excluded field, by name. `mode:` is the one clause (6) writes and the defect
#: this clause was written for; the other three are the rest of the tick-written metadata
#: the audit does not read.
EXCLUDED = ("mode:", "last_auto_resume", "claude_session_id", "metrics_sessions")

#: (d) — the cut, stated as a rule rather than a list. Without it the four names above read
#: as an arbitrary sample and the next reader extends them.
CUT_IS_THE_RULE = "the fields whose value the audit does not read"

#: (d), the consequence, so a reader who is about to add `phase:` to the exclusion set
#: meets the reason not to before they make the edit.
PHASE_STAYS = "status:` and `phase:` are NOT excluded"


def check(root):
    """Return a list of failure strings for the tree at `root`. Empty means pass."""
    failures = []
    path = root / HOME
    if not path.exists():
        return [f"{HOME}: the key derivation's home is missing"]

    text = path.read_text(encoding="utf-8")

    if CLAUSE_OPEN not in text:
        return [f"{HOME}: clause (1) no longer opens with {CLAUSE_OPEN!r} — this check is stale, "
                f"fix it before trusting a pass"]

    # Scope every assertion to clause (1), so a phrase surviving elsewhere in the file — the
    # same words appear in clause (6)'s measured note and in `<output_format>` — cannot
    # satisfy a check on this clause.
    start = text.index(CLAUSE_OPEN)
    nxt = re.search(r"\n\n\*\*\(\d", text[start:])
    clause = text[start:start + nxt.start()] if nxt else text[start:]

    if not ALGORITHM.search(clause):
        failures.append(
            f"{HOME} clause (1): the derivation states no `sha256 … 16 hex` formula — a home "
            f"without an algorithm is one each leg finishes itself, which is the drift this "
            f"guards (three truncation lengths were measured on disk 2026-10-06)"
        )

    for field in EXCLUDED:
        if field not in clause:
            failures.append(
                f"{HOME} clause (1): the excluded-field list omits `{field}` — an excluded "
                f"field the clause does not name is a field the next leg hashes anyway"
            )

    if CUT_IS_THE_RULE not in clause:
        failures.append(
            f"{HOME} clause (1): the cut is not stated as *the fields whose value the audit "
            f"does not read* — without the rule the four names read as a sample, and the next "
            f"reader extends them to everything a tick writes"
        )

    if PHASE_STAYS not in clause:
        failures.append(
            f"{HOME} clause (1): the clause no longer says `status:` / `phase:` are NOT "
            f"excluded — excluding them freezes a row's verdict label across an approval, "
            f"since a `phase` move from `todo` to `planning` is what turns "
            f"`✅ Ready for approval` into `🚀 Ready`"
        )

    return failures


def main():
    failures = check(ROOT)

    if failures:
        for f in failures:
            print(f"  content-key-formula FAIL: {f}", file=sys.stderr)
        sys.exit(1)

    print(
        f"  content-key-formula ok: {HOME} clause (1) states the derivation "
        f"(sha256[:16]), names all {len(EXCLUDED)} excluded fields, states the cut as a rule, "
        f"and keeps `status:` / `phase:` in the key"
    )


if __name__ == "__main__":
    main()
