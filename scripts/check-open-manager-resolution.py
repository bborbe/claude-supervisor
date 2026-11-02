#!/usr/bin/env python3
"""A subject's manager is resolved from the page field BEFORE the registry result, never
after it — because an empty registry match cannot tell "no manager ever existed" from
"the manager exited".

`session-liveness.py --list --json` lists live sessions only, so the same empty output means
both things. The page's `manager_session_id` is what distinguishes them, and a read gated on
the registry result never runs for the case it exists to catch. Measured 2026-10-27: a Fleet
Manager reached Step 2B item 4 (spawn) on a goal whose page carried
`manager_session_id: 276fc353-…`, which `--check` resolves `ABSENT` — a verdict item 3 maps
to RESUME.

This check asserts the three things that keep that from recurring in `commands/open.md`:

  (a) both branches read the page field **unconditionally**, before the registry result
  (b) the empty-match **ambiguity is stated** where a caller reads it
  (c) the spawn arm stays reachable for a subject that **never had a manager** — `None`
      means registry empty AND no page id, never registry empty alone

⚠️ **What this check does not prove.** It is a prose-shape check over a markdown command
file, because a command file *is* prose — there is no function to call and no return value to
thread. So it cannot prove the resolution is *performed*; it proves the text cannot silently
lose the unconditional read or re-widen the spawn arm. The behavioural half is one real
`/supervisor:open --goal` run against an `ABSENT` page id, which no grep can fake.

⚠️ **The superseded wording is pinned as a literal denial.** The old text is what regresses
when a rewrite copies the branch back from an older revision, so its presence is a failure
rather than an absence.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMMAND = "commands/open.md"

#: Section boundaries. A renamed heading fails loudly instead of scanning an empty string and
#: reporting a clean branch — the failure mode that makes a prose check worthless.
SECTIONS = (
    ("Step 2B", "## Step 2B — Goal branch", "## Step 2C — Topic branch"),
    ("Step 2C", "## Step 2C — Topic branch", "## Step 3 — Spawn the tab"),
)

#: The read must be unconditional. Same sentence shape in both branches, so one marker
#: covers (a) — but it is asserted per section, because "updated one of two" is the defect.
UNCONDITIONAL_MARKER = "UNCONDITIONALLY"

#: The ambiguity, stated per branch. 2B was the gap; 2C is asserted too so a future rewrite
#: cannot silently drop it from the branch that already had it.
AMBIGUITY_MARKERS = {
    "Step 2B": "An empty match is never",
    "Step 2C": "an empty match is **ambiguous**",
}

#: (c) — the replacement branch. Without it, a subject that never had a manager has no spawn
#: route at all, which is a worse regression than the duplicate spawn this change fixes.
SPAWN_SCOPE_MARKER = "registry empty AND the page carried no `manager_session_id`"

#: Literal denial pins — the superseded wording. Any of these present means the fix was
#: reverted, half-applied, or copied back from an older revision.
DENIED = (
    "None, `UNKNOWN` or `AMBIGUOUS`",
    "When the registry matches nothing, read it",
    "When it matches nothing, read the topic page",
)


def section(text, start, end):
    """The slice between two headings, or None when either heading is missing."""
    i = text.find(start)
    if i < 0:
        return None
    j = text.find(end, i + len(start))
    if j < 0:
        return None
    return text[i:j]


def check(root):
    """Return the list of failures for the tree at `root`. Empty means pass."""
    path = root / COMMAND
    if not path.exists():
        return [f"{COMMAND}: missing — the command this check guards is gone"]

    text = path.read_text(encoding="utf-8")
    failures = []

    for name, start, end in SECTIONS:
        body = section(text, start, end)
        if body is None:
            failures.append(
                f"{COMMAND}: no {name} section between {start!r} and {end!r} — the branch was "
                f"renamed, retitled or removed, so every assertion below would scan nothing"
            )
            continue

        if UNCONDITIONAL_MARKER not in body:
            failures.append(
                f"{COMMAND} {name}: the page read is not marked {UNCONDITIONAL_MARKER} — a read "
                f"gated on the registry result never runs for an exited manager, which is the "
                f"case it exists to catch"
            )

        ambiguity = AMBIGUITY_MARKERS[name]
        if ambiguity not in body:
            failures.append(
                f"{COMMAND} {name}: never states the empty-match ambiguity (`{ambiguity}`) — "
                f"without it an empty result reads as 'no manager ever existed'"
            )

        if SPAWN_SCOPE_MARKER not in body:
            failures.append(
                f"{COMMAND} {name}: the spawn arm does not scope `None` to "
                f"`{SPAWN_SCOPE_MARKER}` — a subject that never had a manager would have no "
                f"spawn route left"
            )

        for denied in DENIED:
            if denied in body:
                failures.append(
                    f"{COMMAND} {name}: carries the superseded wording `{denied}` — the "
                    f"conditional read or the bare spawn enumeration is back"
                )

    return failures


def main():
    failures = check(ROOT)

    if failures:
        for f in failures:
            print(f"  open-manager-resolution FAIL: {f}", file=sys.stderr)
        sys.exit(1)

    print(
        f"  open-manager-resolution ok: {len(SECTIONS)} branch(es) read the page field "
        f"{UNCONDITIONAL_MARKER.lower()}, state the empty-match ambiguity, and keep the "
        f"no-page-id spawn arm"
    )


if __name__ == "__main__":
    main()
