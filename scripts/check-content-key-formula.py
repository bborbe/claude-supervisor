#!/usr/bin/env python3
"""The drive leg's `content_key` derivation must stay stated, and must keep excluding
the fields the audit does not read.

`agents/goal-drive.md` clause (1) is the single home of the key's derivation. Until
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

**What this check proves, and what it still cannot.** Until 2026-10-13 this was a
prose-shape check over markdown *only*, because the derivation was executed by an agent —
there was no function to call and no return value to assert on, so it could not prove the
leg *computed* the key this way. Its own docstring said so, and the defect it could not
reach arrived anyway: measured **2026-10-12T15:49:30Z**, a drive leg's own `Audit` block
reported that its stored keys reproduced under a `mode:`-only exclusion while the clause
named four fields — so the writer and the reader keyed the same row differently and the row
read as cold on every tick. The derivation now has a **single home in code** (`content_key`
in `scripts/manager-predispatch.py`), the clause points at it, and this check asserts both
halves:

  * the clause still **names the call** (`--content-keys`) and still carries the four
    excluded fields and the `status:` / `phase:` carve-out — so a reader never has to open
    the script to know what the key ignores, and never re-implements it from prose; and
  * the script's **constants** — the four excluded names and the 16-hex truncation — which
    a prose-only check explicitly could not reach.

The derivation's *behaviour* (a tick-written field does not move the key, `phase:` does, the
exclusion reaches the continuation block, the hash is over raw bytes) is asserted by
`scripts/tests/test_content_key.py`. The end-to-end half remains the live-tick measurement —
a row prepared on tick N reporting a cache hit on tick N+1 — which no grep can fake.
"""
import importlib.util
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOME = "agents/goal-drive.md"

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

#: (g) — the **single home**. The clause must name the call that computes the key. A clause
#: that states a formula without naming its implementation is one each leg finishes itself,
#: which is the drift this whole check exists to end — and the drift that arrived anyway on
#: 2026-10-12, while the clause was asserting every one of (a)–(f).
SINGLE_HOME_CALL = "--content-keys"

#: (g) — and it must say the derivation is not the reader's to write. The call alone reads
#: as an option; this is the instruction.
SINGLE_HOME_RULE = "never derived by you"

#: (h) — the script holding the derivation, and the two constants read back from it. ⚠️
#: **This is the half the prose-only check explicitly could not reach** (see the docstring):
#: with the derivation in code there are constants to assert against, so a later edit that
#: widens the exclusion set or changes the truncation fails here rather than in production.
SCRIPT_HOME = "scripts/manager-predispatch.py"

#: (h) — the excluded fields as the SCRIPT must carry them, bare names without their colons.
#: Kept separate from `EXCLUDED` above, which is the markdown form: the two are read from
#: different artifacts and a drift between them is exactly what this pair is here to catch.
SCRIPT_EXCLUDED = ("mode", "last_auto_resume", "claude_session_id", "metrics_sessions")

#: (h) — the truncation the script must apply.
SCRIPT_HEX = 16


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

    # (g) — the single home, stated as a call. ⚠️ **This is the assertion whose absence let
    # the 2026-10-12 drift through.** Every (a)–(f) assertion above was green while a leg
    # implemented a different exclusion set, because a clause can state a formula perfectly
    # and still be re-implemented by the agent reading it.
    if SINGLE_HOME_CALL not in clause:
        failures.append(
            f"{HOME} clause (1): the clause does not name `{SINGLE_HOME_CALL}` — without the "
            f"call a leg has no implementation to reach for and writes its own, which is the "
            f"per-leg drift measured on 2026-10-12 (a `mode:`-only exclusion against this "
            f"clause's four fields) while every other assertion here stayed green"
        )

    if SINGLE_HOME_RULE not in clause:
        failures.append(
            f"{HOME} clause (1): the clause no longer says the derivation is "
            f"`{SINGLE_HOME_RULE}` — a call that reads as an option is one a leg may skip"
        )

    # (h) — the constants, read back from the script. ⚠️ **Import it; never grep it.** A text
    # scan for `CONTENT_KEY_EXCLUDED = (...)` passes on a commented-out line or a docstring
    # example, which is the same class of false green this check's own history is made of.
    script_path = root / SCRIPT_HOME
    if not script_path.exists():
        failures.append(f"{SCRIPT_HOME}: the derivation's single home is missing")
    else:
        spec = importlib.util.spec_from_file_location("_content_key_checked", script_path)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # any load failure is the same verdict on this path
            failures.append(f"{SCRIPT_HOME}: cannot be loaded to read its key constants: {exc}")
        else:
            got = tuple(getattr(module, "CONTENT_KEY_EXCLUDED", ()))
            if got != SCRIPT_EXCLUDED:
                failures.append(
                    f"{SCRIPT_HOME}: CONTENT_KEY_EXCLUDED is {got!r}, not {SCRIPT_EXCLUDED!r} — "
                    f"the script's exclusion set and the clause's list are the two halves of "
                    f"one contract, and a drift between them is the 2026-10-12 defect"
                )
            hex_len = getattr(module, "CONTENT_KEY_HEX", None)
            if hex_len != SCRIPT_HEX:
                failures.append(
                    f"{SCRIPT_HOME}: CONTENT_KEY_HEX is {hex_len!r}, not "
                    f"{SCRIPT_HEX} — a key written at one length can never match another, so "
                    f"a drifted subject re-audits every one of its rows"
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
