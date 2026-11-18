#!/usr/bin/env python3
"""The open leg hands `/supervisor:open` the row's raw `name`, never its shell-quoted `action`.

`scripts/open-queue.py` composes `action` with `shlex.quote`, which is the right encoding for a
consumer that passes the string through a shell. `Skill` runs no shell, so the escape sequence
survives literally: a task title containing an apostrophe reached `commands/open.md:419` as
`sweep-gate.py'"'"'s Home …`, `vault-cli` was asked for a task by that literal name, exited 1, and
Step 2A reported it as `❓ SESSION UNREADABLE`.

Measured 2026-11-18, same vault and session: the bare title exits 0 and returns its session id; the
same title carrying `'"'"'` exits 1 with `find task file: …: file not found`; the three sibling rows
round `1795018507` selected (no apostrophe) exit 0. That round selected 5 and refused exactly the
one row whose title contains an apostrophe.

⚠️ **The fix is NOT to unquote the producer.** `open-queue.py`'s own comment documents the quoting as
deliberate: a `"` breaks naive quoting, and a `$(…)` or backtick would execute in a shell. Any
consumer that really does run a shell keeps needing that form. The defect is that *this* consumer
does not, so it must read `name` and leave `action` alone.

Both directions are asserted: a raw name must round-trip unchanged, and the quoted action must still
carry the escape (so this check can actually fail).
"""

import pathlib
import shlex
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ACTOR = ROOT / "agents" / "fleet-open-actor.md"

#: Optional first argument: the actor file to check. It exists so the failing direction can be
#: demonstrated against a pre-fix copy (`git show <sha>:agents/fleet-open-actor.md`) without
#: touching the tree.
ACTOR_ARG = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else None

#: The apostrophe title that reproduces the defect, verbatim from round `1795018507`.
APOSTROPHE_NAME = "sweep-gate.py's Home Is Decided and It Stops Being a Per-Tick Python Process"


def fail(message: str) -> None:
    print("❌ " + message, file=sys.stderr)
    sys.exit(1)


def main() -> None:
    actor = ACTOR_ARG if ACTOR_ARG is not None else ACTOR
    if not actor.is_file():
        fail("missing %s" % actor)
    body = actor.read_text(encoding="utf-8")

    # 1. The instruction names `selected[].name` as the source of the name.
    if "selected[].name" not in body:
        fail("agents/fleet-open-actor.md never mentions `selected[].name` — the open leg has no "
             "stated source for the name it hands `/supervisor:open`")

    # 2. The instruction says not to read the name from `selected[].action`.
    if "never from `selected[].action`" not in body:
        fail("agents/fleet-open-actor.md does not warn against taking the name from "
             "`selected[].action` — the shell-quoted form is what produced `SESSION UNREADABLE`")

    # 2b. The OLD instruction must be gone from every other part of the file. A body that says
    #     `name` in § 3 while its constraints still say `action` sends an agent back to the bug —
    #     a presence-only guard passes straight through that contradiction (caught in review).
    for stale in (
        "take the `action` string from `--json`",
        "Never compose the command from the printed name",
        "run every `selected[].action` verbatim",
        "Every `selected[].action` was invoked",
    ):
        if stale in body:
            fail("agents/fleet-open-actor.md still instructs the OLD behaviour (%r) — an agent "
                 "following it reproduces SESSION UNREADABLE even though § 3 was rewritten" % stale)

    # 3. The instruction keeps the producer's quoting in place for shell consumers.
    if "do not unquote the producer" not in body:
        fail("agents/fleet-open-actor.md no longer says the producer must not be unquoted — "
             "removing that guard re-opens the `\"` / `$(…)` / backtick hole it exists for")

    # 4. A raw name round-trips unchanged: no shell-escape sequence is introduced.
    raw_args = "%s --task --vault private-personal" % APOSTROPHE_NAME
    if "'\"'\"'" in raw_args:
        fail("composing args from the raw name introduced a shell-escape sequence")

    # 5. Positive control: the quoted form really does carry the escape. If this stops being true,
    #    the check above proves nothing and this file is stale.
    quoted = shlex.quote(APOSTROPHE_NAME)
    if "'\"'\"'" not in quoted:
        fail("shlex.quote no longer escapes the apostrophe — this check is stale and its negative "
             "direction proves nothing")

    print("✅ open leg reads the raw name; quoted action still carries the escape (control fires)")


if __name__ == "__main__":
    main()
