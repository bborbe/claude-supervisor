#!/usr/bin/env python3
"""Every new-worker spawn site must reach the mode decision, and the rule itself
must have exactly one home.

The rule lives in `docs/fleet-surface.md` § Spawn a worker **item 6**. This check does
not re-read the rule; it asserts the two things that make it *reachable* from a site:

  (a) the site **references the home** — a pointer, not a copy
  (b) the site carries the **binding in both directions** — `mode: headless` →
      `interactive=false`, and `mode: interactive` → `interactive=true`, with the
      argument omitted only when `mode:` is absent

Both are required, and neither is sufficient alone. (a) without (b) is a pointer nobody
follows; (b) without (a) is a second copy of the rule, which is the defect this whole
change exists to remove. A bare comment naming the section satisfies neither.

**A call is a new-worker spawn iff it carries `prompt=` and NOT `resume=`.** The
`resume=` half is load-bearing, not a nicety: `commands/fleet-loop.md` continues an
exited headless worker with `spawn_agent(prompt="<the answer>", resume="<session-id>",
interactive=false, …)` — it has BOTH keys, and keying on `prompt=` alone would classify
it as a new-worker site and demand the mode decision of a resume. Resumes answer to the
auto-resume gate, not to the mode decision, and `interactive=false` is correct there
for a different reason.

**What this check does not prove.** It is a prose-shape check over markdown, because a
command file *is* prose — there is no function to call and no return value to thread.
So it cannot prove the mode decision is *made*; it proves the site cannot silently
*omit* it. The behavioural half is the ledger measurement (a day's new-worker spawns
showing `mode_source=argument`, at least one `mode=headless`), which no grep can fake.

Measured 2026-09-23, before this check existed: **63 new-worker spawns in one day and
0 of them headless**, 57 sourced from the fleet config rather than from any decision.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCANNED = ("commands", "agents", "docs")

MODE_HOME = "docs/fleet-surface.md"
#: The pointer every non-home site must carry. Deliberately specific: naming the file
#: alone would pass for a file that mentions fleet-surface for any other reason.
MODE_ANCHOR = "Spawn a worker item 6"

#: Files that document opening a NEW worker. Each must reference the home.
#: ⚠️ `agents/manager-drive.md` is listed here even though it no longer opens at all: it
#: **decides** the mode and writes `mode:` to disk, which is the half a text scan can still
#: hold to the rule. Until 2026-09-25 it also *opened* — its `tools:` grant permitted it, and
#: on 2026-09-24 it used that to open two rows directly, writing `mode: interactive` to each
#: task and then omitting the argument, so both reported `mode_source=config`. A site defined
#: by a grant is invisible to a text scan, which is why the grant is gone and the decision
#: stayed.
#: ⚠️ `commands/manager-drive.md` joined on 2026-09-25 for the opposite reason: the spawn
#: moved *out* of the agent and into its caller, so the command that executes the hand-off
#: rows is now a spawn site and must carry the binding.
SPAWN_SITES = (
    "commands/open.md",
    "commands/manager-loop.md",
    "commands/manager-spawn.md",
    "commands/manager-drive.md",
    "agents/manager-drive.md",
)

#: Resume-only sites. Excluded on purpose — they are not new-worker sites and must not
#: be re-classified. Listed so that a *new* file joining this set is a deliberate edit.
RESUME_ONLY = (
    "commands/fleet-loop.md",
)

#: The dimension a text scan cannot reach. A tool grant permits a spawn whether or not the
#: prose describes one, so every file whose frontmatter grants it must at least carry the
#: pointer to the rule's home — otherwise a reader (or an agent) following that file has no
#: route to the rule at all. This is what `agents/manager-drive.md` was missing.
GRANT_TOOL = "mcp__supervisor__spawn_agent"
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)

CALL = re.compile(r"spawn_agent\(")
HEADLESS_CONDITION = re.compile(r"`?mode:`?[^\n]{0,40}headless|headless[^\n]{0,40}`?mode:`?")
INTERACTIVE_FALSE = "interactive=false"
#: The other direction. A site that passes only `false` leaves `mode: interactive` inert — a task
#: that explicitly declared itself interactive flips to headless the moment the fleet default
#: moves, and its omitted argument reports `mode_source=config`, making a wired spawn
#: indistinguishable from an unwired one in the ledger.
INTERACTIVE_TRUE = "interactive=true"
#: The home must actually define the rule, not merely name it.
HOME_DEFINES = ("does finishing this task raise questions mid-flight", "floor, not a tie-break")


def call_spans(text):
    """Yield each `spawn_agent(...)` call as text, paren-balanced and multi-line aware."""
    for m in CALL.finditer(text):
        depth, i = 1, m.end()
        while i < len(text) and depth:
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
            i += 1
        yield text[m.start():i]


def is_new_worker(span):
    return "prompt=" in span and "resume=" not in span


def check(root):
    """Return a list of failure strings for the tree at `root`. Empty means pass."""
    failures = []
    sites_with_calls = set()

    for sub in SCANNED:
        for path in sorted((root / sub).rglob("*.md")):
            rel = str(path.relative_to(root))
            text = path.read_text(encoding="utf-8")

            # The grant dimension runs before the call-shape filter: a file that can open a
            # worker must point at the rule even when its prose shows no `spawn_agent(prompt=`
            # call at all, which is exactly how `agents/manager-drive.md` bypassed the wiring.
            fm = FRONTMATTER.match(text)
            if fm and GRANT_TOOL in fm.group(1) and rel not in SPAWN_SITES and MODE_ANCHOR not in text:
                failures.append(
                    f"{rel}: its frontmatter grants `{GRANT_TOOL}` — which permits opening a "
                    f"worker — but it carries no reference to the rule's home ({MODE_ANCHOR!r}), "
                    f"so a reader following this file has no route to the mode rule"
                )

            if not any(is_new_worker(s) for s in call_spans(text)):
                continue
            sites_with_calls.add(rel)
            if rel in RESUME_ONLY:
                failures.append(
                    f"{rel}: carries a new-worker `spawn_agent(` call but is listed "
                    f"RESUME_ONLY — one of the two is wrong"
                )
            if rel in SPAWN_SITES or rel == MODE_HOME:
                continue
            failures.append(
                f"{rel}: documents a new-worker `spawn_agent(` call but is not a known "
                f"spawn site — add it to SPAWN_SITES and wire it, or to RESUME_ONLY"
            )

    for rel in SPAWN_SITES:
        path = root / rel
        if not path.exists():
            failures.append(f"{rel}: listed as a spawn site but does not exist")
            continue
        text = path.read_text(encoding="utf-8")
        if MODE_ANCHOR not in text:
            failures.append(f"{rel}: no reference to the rule's home ({MODE_ANCHOR!r})")
        if not HEADLESS_CONDITION.search(text):
            failures.append(f"{rel}: no `mode: headless` condition — the binding is missing")
        if INTERACTIVE_FALSE not in text:
            failures.append(f"{rel}: never passes `{INTERACTIVE_FALSE}` — the binding is inert")
        if INTERACTIVE_TRUE not in text:
            failures.append(
                f"{rel}: never passes `{INTERACTIVE_TRUE}` — `mode: interactive` is unhonoured, "
                f"so it flips with the fleet default and reports `mode_source=config`"
            )

    home_path = root / MODE_HOME
    if not home_path.exists():
        failures.append(f"{MODE_HOME}: the rule's home is missing")
        return failures, sites_with_calls
    home = home_path.read_text(encoding="utf-8")
    for phrase in HOME_DEFINES:
        if phrase not in home:
            failures.append(f"{MODE_HOME}: the rule's home no longer defines it ({phrase!r} missing)")

    return failures, sites_with_calls


def main():
    failures, sites_with_calls = check(ROOT)

    if failures:
        for f in failures:
            print(f"  spawn-mode FAIL: {f}", file=sys.stderr)
        sys.exit(1)

    print(
        f"  spawn-mode ok: {len(SPAWN_SITES)} sites reference {MODE_HOME} item 6 and bind "
        f"the mode both ways (`headless`→false, `interactive`→true); "
        f"{len(sites_with_calls)} new-worker call site(s) accounted for"
    )


if __name__ == "__main__":
    main()
