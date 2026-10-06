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

#: (a) and (b) — the algorithm and the truncation, stated **in one sentence**. A formula
#: without a truncation is a formula each leg finishes itself, which is the drift this
#: guards.
#:
#: ⚠️ **Scoped to a sentence, not matched with a regex over the whole clause.** The first
#: version was `re.compile(r"sha256.*?16 hex", re.S)`, whose lazy span is unbounded under
#: DOTALL: it is satisfied by the word `sha256` anywhere in the clause and the words
#: `16 hex` anywhere later, with no adjacency required. It passed for the right reason on
#: the day it shipped — `16 hex` occurred exactly once — but a later sentence mentioning
#: another length ("one leg wrote 16 hex, another 12") would satisfy it from anywhere.
#: Requiring both tokens in the *same sentence* is what makes the assertion mean what its
#: failure message says. Raised by the maintainer bot on PR #172.
ALGORITHM_TOKENS = ("sha256", "16 hex")

#: (c) — the sentence that lists the excluded fields. Scoping matters: the clause's own
#: measured note contains `` `mode: interactive` ``, so a bare `"mode:" in clause` substring
#: test stays green after `mode:` is deleted from the exclusion sentence — for the one field
#: the whole fix depends on. Raised by the maintainer bot on PR #172.
EXCLUSION_OPENS = "The excluded fields are"

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

#: (e) — the **block extent**. `metrics_sessions` is written as a multi-line YAML block in this
#: vault, so a deletion that drops only the `metrics_sessions:` key line leaves its indented
#: `- session_id: <uuid>` entries behind — and those uuids change on every worker run, so the
#: key churns per tick and the exclusion buys nothing. Raised as a CRITICAL by the maintainer
#: bot on PR #172, after the first version of this clause stated a line deletion and stopped
#: there.
BLOCK_EXTENT = "continuation block"

#: (f) — the **removal mechanism**. The clause states which fields go and how far the deletion
#: reaches; this is *which route performs it*. Raw byte deletion and YAML parse-and-re-serialise
#: disagree on CRLF, on the blank line a deleted entry leaves behind, on quoted and multi-line
#: values, and on a trailing newline — so a later edit swapping one for the other leaves every
#: other assertion green while reintroducing exactly the per-leg drift this clause exists to
#: close. Raised as a Should-Fix by the maintainer bot on PR #172, third round: the clause and
#: the CHANGELOG both called the mechanism pinned while nothing asserted it.
MECHANISM = "no YAML parse, no re-serialisation, no newline normalisation"


def sentences_containing(text, token):
    """Return every sentence of `text` carrying `token`.

    Split on a period followed by whitespace. Blunt, and deliberately so: the clause is written
    in sentences, and the point of the split is to bound how far apart two tokens may sit, not
    to parse English.

    ⚠️ **Every sentence, not the first.** Returning the first match makes the guard fail for the
    wrong reason as soon as an earlier sentence mentions `sha256` without its truncation — a
    measured note, a debugging aside — and the failure message ("no single sentence states
    both") would then name the opposite problem from the one on disk. Raised by the maintainer
    bot on PR #172.
    """
    return [s for s in re.split(r"(?<=\.)\s+", text) if token in s]


def exclusion_sentences(text):
    """Return every sentence that opens with the exclusion anchor.

    Scoped to those sentences because the clause also *mentions* `mode:` elsewhere — its own
    measured note quotes `mode: interactive` — so a whole-clause substring test stays green
    after `mode:` is deleted from the list it is supposed to be in.

    ⚠️ **Every match, not the first**, which is the policy `sentences_containing` already
    follows, and for the same reason: returning the first match means a decoy sentence carrying
    the anchor turns the guard red with a message naming the wrong problem — the diagnosis-
    quality defect the `sentences_containing` fix was made to remove. Two helpers disagreeing
    about matching policy for no stated reason is how that asymmetry comes back. Raised by the
    maintainer bot on PR #172, third round.
    """
    return [s for s in re.split(r"(?<=\.)\s+", text) if EXCLUSION_OPENS in s]


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
    if not nxt:
        return [f"{HOME}: clause (1) has no following `**(2)` heading to bound it — this check "
                f"is stale. Without the bound the window widens to the rest of the file, so "
                f"every assertion below could be satisfied by a later clause and the check would "
                f"report green against text that is not the clause it guards. Fix the bound "
                f"before trusting a pass"]
    clause = text[start:start + nxt.start()]

    if not any(
        ALGORITHM_TOKENS[1] in s for s in sentences_containing(clause, ALGORITHM_TOKENS[0])
    ):
        failures.append(
            f"{HOME} clause (1): no single sentence states both `{ALGORITHM_TOKENS[0]}` and "
            f"`{ALGORITHM_TOKENS[1]}` — a home without an algorithm, or an algorithm without "
            f"its truncation, is one each leg finishes itself, which is the drift this guards "
            f"(three truncation lengths were measured on disk 2026-10-06)"
        )

    exclusions = exclusion_sentences(clause)
    if not exclusions:
        failures.append(
            f"{HOME} clause (1): no sentence opens with {EXCLUSION_OPENS!r}, so the "
            f"excluded-field list has been reworded or removed — this check is stale, fix it "
            f"before trusting a pass"
        )
    else:
        for field in EXCLUDED:
            if not any(field in s for s in exclusions):
                failures.append(
                    f"{HOME} clause (1): the exclusion sentence omits `{field}` — an excluded "
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

    if BLOCK_EXTENT not in clause:
        failures.append(
            f"{HOME} clause (1): the removal no longer states a *{BLOCK_EXTENT}* — "
            f"`metrics_sessions` is a multi-line YAML block, so dropping only its key line "
            f"leaves the indented `- session_id:` entries in the key, and those uuids change "
            f"on every worker run: the key churns per tick and the exclusion buys nothing"
        )

    if MECHANISM not in clause:
        failures.append(
            f"{HOME} clause (1): the removal no longer pins its *mechanism* — without "
            f"`{MECHANISM}` a later edit can swap raw byte deletion for a YAML "
            f"parse-and-re-serialise, which disagrees on CRLF, on the blank line a deleted "
            f"entry leaves behind, on quoted and multi-line values, and on a trailing newline, "
            f"while every other assertion here stays green"
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
        f"keeps `status:` / `phase:` in the key, and pins the removal to a raw-byte line "
        f"deletion that takes each key's continuation block with it and parses no YAML"
    )


if __name__ == "__main__":
    main()
