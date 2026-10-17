#!/usr/bin/env python3
"""Every call site that reads a task's `target:` must reach the eligibility rule, and the
rule itself must have exactly one home.

The rule lives in `docs/fleet-surface.md` § Spawn a worker **item 7**. This check does not
re-read the rule; it asserts the three things that make it *reachable* from a site:

  (a) the site **references the home** — a pointer, not a copy
  (b) the site carries the **warning that names the disqualifier**
  (c) the site carries the **advisory clause** — the half that stops it refusing the spawn
      or rewriting the row

(a) without (b) and (c) is a pointer nobody follows; (b) and (c) without (a) is a second copy
of the rule, which is the defect the single-home rule forbids. A bare comment naming the
section satisfies none of them.

⚠️ **What this check does not prove.** It is a prose-shape check over markdown, because a
command file *is* prose — there is no function to call and no return value to thread. So it
cannot prove the eligibility decision is *made*; it proves the site cannot silently *omit* it.

⚠️ **And it covers CITING sites only.** A site that reads `target:` while omitting the item-7
citation is invisible here: the discovery scan keys on the citation, and (b) and (c) are
asserted only for registered sites. The behavioural half is a row actually being classified
and warned about, which no grep can fake.

⚠️ **The registered set is asserted, not assumed.** Discovery scans `commands/` and `agents/`
for the citation, so a fourth site that cites item 7 fails here until it is registered — and
a registered site that stops citing fails too.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCANNED = ("commands", "agents")

TARGET_HOME = "docs/fleet-surface.md"
#: The pointer every non-home site must carry. Deliberately specific: naming the file alone
#: would pass for a file that mentions fleet-surface for any other reason.
TARGET_ANCHOR = "Spawn a worker item 7"

#: The home is identified by its **section heading plus item**, never by the pointer string.
#: ⚠️ That distinction is load-bearing and was measured on the sibling check: the item-5 gate
#: found its own pointer string at two *other* places in `docs/fleet-surface.md`, so a home
#: check keyed on the pointer passes even after the section it points at has been renamed away.
HOME_HEADING = "## Spawn a worker"
HOME_ITEM = re.compile(r"^7\. \*\*Decide the TARGET", re.M)

#: The two markers the binding is made of, and they are deliberately different sentences: a
#: single marker read for both would let one satisfy the check for the other.
#:
#: ⚠️ **Neither marker is the anchor string, and that separation is the point.** A site can cite
#: item 7 in passing and carry no binding at all — which is precisely the shape this check exists
#: to reject — so (a) the citation and (b)/(c) the two markers are asserted independently, and
#: neither satisfies the other.
DISQUALIFIER_MARKER = "is not eligible for"
ADVISORY_MARKER = "Advisory, never a refusal"

#: The sites that read a task's `target:` and carry the rule's binding.
#: ⚠️ Three, and `commands/open-a2a.md` joined on 2026-10-17: it routes every spawn through the
#: a2a-worker-controller, and it reads the field itself (§ 2.1) rather than inheriting the check
#: from `open.md`. It is the site no gate covered before this one existed.
CITING_SITES = (
    "commands/steps/manager-spawn.md",
    "commands/open.md",
    "commands/open-a2a.md",
)


def check(root):
    """Return (failures, discovered) for the tree at `root`. Empty failures means pass."""
    failures = []
    discovered = set()

    for sub in SCANNED:
        for path in sorted((root / sub).rglob("*.md")):
            rel = str(path.relative_to(root))
            if TARGET_ANCHOR in path.read_text(encoding="utf-8"):
                discovered.add(rel)

    for rel in sorted(discovered):
        if rel not in CITING_SITES:
            failures.append(
                f"{rel}: cites `{TARGET_ANCHOR}` but is not a registered target-reading site "
                f"— add it to CITING_SITES and wire it, or drop the citation"
            )

    for rel in CITING_SITES:
        path = root / rel
        if not path.exists():
            failures.append(f"{rel}: listed as a target-reading site but does not exist")
            continue
        text = path.read_text(encoding="utf-8")
        if TARGET_ANCHOR not in text:
            failures.append(f"{rel}: no reference to the rule's home (`{TARGET_ANCHOR}`)")
        if DISQUALIFIER_MARKER not in text:
            failures.append(
                f"{rel}: never names the disqualifier (`{DISQUALIFIER_MARKER}`) — a site that "
                f"cannot say what disqualifies a row cannot warn about one"
            )
        if ADVISORY_MARKER not in text:
            failures.append(
                f"{rel}: carries no advisory clause (`{ADVISORY_MARKER}`) — without it the rule "
                f"reads as a refusal, and a misclassified row loses its route to a local spawn"
            )

    home_path = root / TARGET_HOME
    if not home_path.exists():
        failures.append(f"{TARGET_HOME}: the rule's home is missing")
        return failures, discovered

    home = home_path.read_text(encoding="utf-8")
    if HOME_HEADING not in home:
        failures.append(
            f"{TARGET_HOME}: no {HOME_HEADING!r} section — the rule's home is gone, so every "
            f"pointer to it now points at nothing"
        )
    elif not HOME_ITEM.search(home):
        failures.append(
            f"{TARGET_HOME}: {HOME_HEADING!r} exists but carries no item 7 for the target "
            f"decision — the section was renumbered or retitled, and every `{TARGET_ANCHOR}` "
            f"pointer into it now lands somewhere else"
        )

    return failures, discovered


def main():
    failures, discovered = check(ROOT)

    if failures:
        for f in failures:
            print(f"  spawn-target FAIL: {f}", file=sys.stderr)
        sys.exit(1)

    print(
        f"  spawn-target ok: {len(CITING_SITES)} target-reading site(s) cite {TARGET_HOME} "
        f"item 7 and carry the binding (disqualifier warning + `{ADVISORY_MARKER}`); "
        f"{len(discovered)} citing site(s) accounted for"
    )


if __name__ == "__main__":
    main()
