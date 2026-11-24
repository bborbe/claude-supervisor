#!/usr/bin/env python3
"""A task's session-id set, read by FIELD — and a refusal when a `task_identifier`
reaches it.

The manager's liveness probe needs a task's session ids: the frontmatter
`claude_session_id` plus every `metrics_sessions` id. Read **by field**, that set is
exactly the real sessions. Read by **shape** — any bare uuid in the file — it also sweeps
in `task_identifier`, a uuid of exactly the same shape carried by nearly every task file,
which hands a task with no session a **phantom owner** and silently drops it out of
`ready-to-start`.

`commands/goal-loop.md` forbade the shape match in prose on 2026-10-05. Prose is
executed by the same model the rule constrains, so it holds only while the model
remembers to apply it — that is not a check. **This script is the check, and the probe
calls it for the set instead of extracting in prose.** The extraction leaves the model's
hands, so the phantom shape stops being discouraged and becomes unreachable: there is no
prose extraction left to get wrong, and no shape mode to reach for.

Measured 2026-09-30 on the BRO-21389 tracked set (35 tasks, `~/.claude/state/
sweep-gate-loop/brogrammers/bro-21389-mdm-via-rest.snapshot.history.jsonl`): a bare-uuid
shape match over the frontmatter yielded a **54-id population, 34 of them
`task_identifier` phantoms, with 11 tasks carrying a session id**. The shape match sweeps
in exactly one `task_identifier` per tracked task, which is why the phantom count tracks
the tracked-set size rather than any property of the tasks.

Two refusals, both `REJECTED:` on stderr and exit 1. It REFUSES; it never merely omits:

  * an id in the set equal to the file's own `task_identifier` — the phantom shape
    reached from a real file. A field-scoped read cannot produce this, so it firing means
    the extraction changed under us, and a silent omission would report a clean set for a
    file it read wrongly.
  * `--shape` — the prohibited mode itself. The script has no shape mode; asking for one
    is refused with the reason rather than answered.

⚠️ **A refusal is a verdict about the extraction, not a cue to read the file yourself.**
Neither the manager nor the sweep reader may fall back to a hand-read set — a hand read is
exactly the prose extraction this call replaces, and it is the one that gets the phantom
shape wrong. Report the row with no id set and say the script refused. The command files
carry the same rule (`commands/goal-loop.md`, `commands/goal-status.md`), so the
enforced behaviour and the stated rule agree rather than each holding half.

⚠️ **The extractor is imported, never re-implemented.** `session_id_set()` in
`scripts/manager-predispatch.py` is the one home for "which fields are a session id"; a
second copy here would be the second counter a `grep` cannot tell from a real one, and
the two would drift silently in the direction that admits a phantom.

Usage:
  session-ids.py --task <task-file>     # the id set, one per line, in frontmatter order
  session-ids.py --frontmatter <file>   # the same, for any file carrying frontmatter
  session-ids.py --task <file> --owns <session-id>   # does this page OWN that session?
  session-ids.py --shape <anything>     # refused, exit 1

**`--owns` is the reap join's guard, and it exists because that join was performed in
prose.** A fleet round resolved a live manager session to a COMPLETED task page and
drafted a reap for it, because the page's BODY carried a sentence naming the manager's id
— ``claude_session_id: c49298a8-…`` written as a routing instruction in `# Tasks`, 18 050
bytes into the file. A stamp is a FRONTMATTER field; a body mention of the same shape is
prose, and reading the two alike is the prose-extraction defect this script's id set
already exists to remove. Measured 2026-11-23, round `1795464184.784114`:
`A Worker That Dies Mid-Turn Becomes Unresumable.md` is `completed · done · 0 open` and
stamps `7daf8265…` in frontmatter, while the reap was drafted for `c49298a8…` — a healthy
goal manager at 2/3 SC, told it was finished and pointed at `/exit`.

So the reap decision asks this script, per row: the page the sweep joined must itself
carry the session in its **frontmatter** id set. A page whose frontmatter names a
different session does not own the one being reaped, whatever its body says, and the row
is `unverifiable` rather than `finished`. Pass the FULL id — a prefix matches nothing and
therefore fails closed, which is the correct direction for a guard that gates a reap.

Exit: 0 ok / OWNS · 1 REJECTED · 2 unreadable/usage · 3 NOT-OWNS
"""
import argparse
import importlib.util
import os
import sys

SCRIPTS = os.path.dirname(os.path.abspath(__file__))

# The field set the probe may read, named here so `commands/goal-loop.md`'s guard
# bullet can name the same set this script enforces. `task_identifier` is deliberately
# ABSENT — its absence is the whole point, and a reader should see the field the guard is
# about without having to read the extractor.
SESSION_ID_FIELDS = ("claude_session_id", "metrics_sessions[].session_id")

# The uuid shapes that must never count as a session id. Named rather than implied, for
# the same reason: the guard's subject should be visible at the top of the file.
PHANTOM_FIELDS = ("task_identifier",)

EXIT_REJECTED = 1
EXIT_UNREADABLE = 2
EXIT_NOT_OWNS = 3


def _load_sibling(name: str):
    """`scripts/<name>.py` as a module — the repo's way of reusing a sibling script
    (`scripts/inbox.py` loads `manager-liveness.py` the same way), so an extractor keeps
    one home instead of being copied."""
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), os.path.join(SCRIPTS, f"{name}.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_PREDISPATCH = _load_sibling("manager-predispatch")


def frontmatter_of(text: str) -> str | None:
    """The file's frontmatter block, or None when it carries none.

    `None` is not `""`: a file with no frontmatter cannot be read for ids, and reporting
    it as an empty set would read as "this task has no session" — the direction that
    silently drops a live row out of `ready-to-start`.
    """
    return _PREDISPATCH.split_frontmatter(text)


def phantom_ids(fm: str) -> list:
    """Every value the file carries in a phantom field, in field order.

    Read from the FRONTMATTER, so a `task_identifier` named in `# Progress` prose is not
    a phantom id — only the field the vault writes can hand a task a phantom owner.
    """
    out = []
    for field in PHANTOM_FIELDS:
        val = _PREDISPATCH.fm_scalar(fm, field).strip().strip("'\"")
        if val and val not in out:
            out.append(val)
    return out


def refusal(fm: str, ids: list) -> str | None:
    """The refusal line when the set is poisoned, else None.

    ⚠️ **This is the guard, and it REFUSES rather than filters.** Dropping the offending
    id would leave a caller holding a set it believes was read correctly — the silent
    direction, and the exact shape this whole change exists to remove. A field-scoped
    read cannot produce one of these, so the guard firing is itself the signal that the
    extraction is not the one this script declares.
    """
    phantoms = phantom_ids(fm)
    for i in ids:
        if i in phantoms:
            return (
                f"REJECTED: {i} is this task's `task_identifier`, not a session id. "
                f"A shape match sweeps it in — a uuid of exactly the same shape, which "
                f"gives a task with no session a PHANTOM OWNER. Read the set by field "
                f"({', '.join(SESSION_ID_FIELDS)}); never by shape."
            )
    return None


def read_ids(path: str) -> tuple:
    """(ids, refusal, error). Exactly one of refusal/error is non-None on a bad read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        return [], None, f"cannot read {path}: {exc}"
    fm = frontmatter_of(text)
    if fm is None:
        return [], None, f"no frontmatter in {path} — cannot read an id set from it"
    ids = _PREDISPATCH.session_id_set(fm)
    return ids, refusal(fm, ids), None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="A task's session-id set, by field, refusing a task_identifier."
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--task", metavar="FILE", help="a task file; its id set is printed")
    src.add_argument(
        "--frontmatter", metavar="FILE", help="any file carrying frontmatter (same read)"
    )
    src.add_argument(
        "--shape",
        metavar="ANY",
        help="REFUSED — the prohibited mode; see the module docstring",
    )
    ap.add_argument(
        "--owns",
        metavar="SESSION_ID",
        help=(
            "with --task: answer whether that page's FRONTMATTER id set names this "
            "session. Prints OWNS (exit 0) or NOT-OWNS (exit 3). Full id only — a "
            "prefix matches nothing and fails closed."
        ),
    )
    a = ap.parse_args(argv)

    if a.shape is not None:
        print(
            "REJECTED: --shape is the prohibited extraction mode. A bare-uuid shape "
            "match sweeps in `task_identifier` — a uuid of exactly the same shape — "
            f"giving a task with no session a phantom owner. Read by field: "
            f"{', '.join(SESSION_ID_FIELDS)}.",
            file=sys.stderr,
        )
        return EXIT_REJECTED

    # `is not None`, never truthiness: the per-task loop this script is written for
    # expands an unset variable to an EMPTY argument, and `a.task or a.frontmatter`
    # would turn that into `None` — `open(None)` raises TypeError, which is not an
    # OSError, so it escapes `read_ids` as a traceback at exit 1: the code this module
    # reserves for REJECTED. An empty path is its own verdict, reported as unreadable.
    path = a.task if a.task is not None else a.frontmatter
    if not path:
        print(
            "UNREADABLE: empty path — pass a task file. An unset shell variable "
            "expands to an empty argument, which names no file.",
            file=sys.stderr,
        )
        return EXIT_UNREADABLE
    ids, refuse, error = read_ids(path)
    if error:
        print(f"UNREADABLE: {error}", file=sys.stderr)
        return EXIT_UNREADABLE
    if refuse:
        print(refuse, file=sys.stderr)
        return EXIT_REJECTED

    if a.owns is not None:
        # The reap join's guard. `--frontmatter` is refused here rather than silently
        # accepted: the guard's question is whether the PAGE owns the session, and a
        # file carrying no task identity is not that page.
        if a.task is None:
            print(
                "UNREADABLE: --owns needs --task <file> — the guard asks whether a TASK "
                "PAGE owns a session, so the page must be named as a task.",
                file=sys.stderr,
            )
            return EXIT_UNREADABLE
        want = a.owns.strip().strip("'\"").lower()
        have = [i.strip().strip("'\"").lower() for i in ids]
        if want in have:
            print("OWNS")
            return 0
        print(
            "NOT-OWNS page-stamp="
            + (" ".join(ids) if ids else "(none)")
            + f" session={a.owns.strip()}"
        )
        return EXIT_NOT_OWNS

    for i in ids:
        print(i)
    return 0


if __name__ == "__main__":
    sys.exit(main())
