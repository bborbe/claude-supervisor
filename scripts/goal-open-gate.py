#!/usr/bin/env python3
"""`/supervisor:open --goal`'s OPEN gate — the flag predicate the topic branch already runs.

The two manager branches were not symmetric. The TOPIC branch (`commands/open.md` § Step 2C
item 4) runs `topic-spawn.py --topic-open` and refuses `topic-unflagged`; the GOAL branch
(§ Step 2B item 4) had no flag gate at all — only item 0's `phase: todo` refusal — so
`/supervisor:open --goal "<goal>"` spawned a manager for ANY goal, whatever its flag. That
is the last path by which an unflagged goal manager could be started, and it re-created the
state the topic tier exists to prevent: measured 2026-11-15, 11 goal managers were running
on goals `flag_admits()` refuses, while `topic-spawn.py --rank` for the topic declaring them
reported 1 startable of 53.

⚠️ **This gates SPAWN only.** RESUME (`§ Step 2B` item 3, a recorded `claude_session_id`
whose session is `ABSENT`) must stay ungated: a lapsed manager on a goal whose flag was
later cleared still has to come back, which is the `stand-down` case
`commands/topic-drive.md` § `leave` already treats as legitimate. The caller reads this
gate at the open site and nowhere earlier.

⚠️ **Provenance is read, not just presence.** The rule is `flag_provenance.flag_admits()`
— `flag: true` AND `flag_set_by` in (`operator`, `legacy`). An agent-set or absent setter
refuses, exactly as on the topic branch and the task batch; a bare `flag: true` written by
hand is NOT an approval.

Exit codes: 0 `open-ok` · 1 a refusal (its own token on the first line) · 2 usage error.
The verdict is the FIRST line, as on `topic-spawn.py`.
"""
import argparse
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

#: The goals folder names, newest first — the same pair `scripts/open-queue.py`
#: (`GOAL_DIRNAMES`) resolves. `private-family`, `private-starcitizen` and `private-boss`
#: still carry `23 Goals`; a single hard-coded `24 Goals` would answer `goal-not-found` for
#: every goal there, refusing a flagged goal as though it did not exist.
GOAL_DIRNAMES = ("24 Goals", "23 Goals")


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: The single predicate. Held as a module attribute because
#: `scripts/tests/test_flag_opens_callers.py` asserts the reference structurally — a copy
#: of the rule that happens to agree today is the failure that test exists to catch.
flag_provenance = _load("flag_provenance", "flag_provenance.py")


def frontmatter(text):
    """The YAML block between the leading `---` pair, or `""` when the page has none."""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return text[3:end] if end != -1 else ""


def flagged(fm, kind="goal"):
    """Does this page's flag admit it? Delegates to the shared predicate."""
    return flag_provenance.flag_admits(fm, kind)


def goal_page(vault, goal):
    """The goal page path, or None when no goals folder holds it.

    Exact `<goal>.md`, never a glob: two goals can share a prefix. The first folder in
    `GOAL_DIRNAMES` that holds the page wins.
    """
    for dirname in GOAL_DIRNAMES:
        page = os.path.join(vault, dirname, goal + ".md")
        if os.path.isfile(page):
            return page
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="May /supervisor:open --goal open a manager for this goal?")
    parser.add_argument("--vault", required=True, help="the vault ROOT path")
    parser.add_argument("--goal", required=True, help="the goal page name, without .md")
    args = parser.parse_args(argv)

    page = goal_page(args.vault, args.goal)
    try:
        if page is None:
            raise OSError("no goals folder holds the page")
        with open(page, encoding="utf-8") as handle:
            fm = frontmatter(handle.read())
    except OSError:
        # Not a flag verdict: the caller passed a name that is not a page here. Kept a
        # distinct token so a typo never reads as "the operator did not flag it".
        print("goal-not-found")
        print("no page named %r under %s in %s"
              % (args.goal, " or ".join(GOAL_DIRNAMES), args.vault))
        return 1

    if flagged(fm, "goal"):
        print("open-ok")
        return 0

    reason = flag_provenance.flag_refusal(fm, "goal") or "not flagged"
    print("goal-unflagged: %s" % reason)
    # ⚠️ The remedy names the Vault UI, never a CLI line. `vault-cli goal set` has no
    # `--by` (only `task set` does), so `goal set <g> flag true` writes a flag with no
    # `flag_set_by` — which `flag_admits()` refuses. Printing that command would hand the
    # operator a fix that leaves the goal exactly as refused as before.
    print("flag it first: toggle the goal's flag in the Vault UI — an operator-set flag. "
          "`vault-cli goal set … flag true` records no setter, so it would still refuse.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
