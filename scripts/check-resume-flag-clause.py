#!/usr/bin/env python3
"""The auto-resume gate must admit a flagged row, and only the open path may read provenance.

`agents/goal-drive.md` step 3 states the auto-resume gate as **prose** — a bullet list of
clauses the drive leg checks on disk. No script implements it (`grep -rn "not parked"
scripts/` names other gates), so a doc-lint is the only instrument that can hold it.

The defect this guards: the `not parked` clause read "frontmatter `flag: true` absent",
so a row the operator had flagged — which `commands/open.md` § Step 0.5 treats as the
approval to open — was never resumed. Operator rulings: 2026-10-30 (*"provenance is not
read: `flag_set_by` is a note, not a gate"*) and 2026-11-03 (*"flag means approved"*),
re-affirmed 2026-11-04.

⚠️ **A token grep is not enough, and this check is not one.** Deleting the `flag` token and
adding a new bullet (`- **not operator-flagged**: …`) would pass any text check while the
gate still excluded flagged rows. So the check **parses** the clauses into a predicate and
**evaluates fixture rows through it**:

  1. The `not parked` bullet — exactly one inside the gate — is split into conjuncts, and
     every conjunct must match the grammar below. An unreadable conjunct fails closed: a
     clause this parser cannot read is not a verdict.
  2. Every *other* gate bullet that names a field the fixtures vary (`flag`,
     `flag_set_by`, `approved_by`, `approved_at`) must parse too, and joins the predicate.
  3. The fixtures — operator-, legacy-, agent- and absent-setter flags, and no flag — must
     all be **admitted**. Two positive controls (`phase: human_review`, `status: hold`)
     must be **excluded**, so a predicate emptied to admit everything also fails.
  4. The open path is evaluated on the same fixtures by executing `commands/open.md`
     § Step 0.5's own selector lines (`OPENING_ACTORS=`, `cand=`, `sel=`): it must admit
     only `operator` / `legacy`, and its actor set must equal `scripts/flagged-rows.py`'s.
  5. The ownership statement lives once — `docs/fleet-surface.md` § Approval provenance —
     and all three surfaces name it.

**What this proves, and what it does not.** It proves the gate's written predicate admits
flagged rows and reads no provenance, and that the open selector as written still refuses
an agent-set flag. It cannot prove a manager follows the prose; nothing executes it.

Scoped to `agents/goal-drive.md` on purpose: `commands/steps/goal-spawn.md`'s own `not
parked` bullet carries `flag: true` in explanatory prose, and a repo-wide scan would fail
a correct file.
"""

import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

GATE_FILE = "agents/goal-drive.md"
OPEN_FILE = "commands/open.md"
RESOLVER = "scripts/flagged-rows.py"
OWNER_DOC = "docs/fleet-surface.md"
SECTION = "Approval provenance"
POINTER_CARRIERS = (GATE_FILE, OPEN_FILE, "commands/steps/goal-spawn.md")

GATE_START = "3. **Then run the auto-resume gate on confirmed orphans**"
GATE_END = "**Gate holds → resume.**"

#: Fields the fixtures vary. A gate bullet naming one — as a backticked field reference,
#: never the English word (the `transcript stale` bullet reads "a bare `stat` flag:") —
#: must be a parseable predicate. The `not parked` bullet is parsed whatever it names.
VARYING = re.compile(r"`(?:flag|flag_set_by|approved_by|approved_at)\b")

BASE = {"status": "in_progress", "phase": "execution"}
APPROVED = {"approved_by": "operator", "approved_at": "2026-11-01T23:57:00+01:00"}
FIXTURES = {
    "operator-flag": {**BASE, **APPROVED, "flag": True, "flag_set_by": "operator"},
    "legacy-flag": {**BASE, **APPROVED, "flag": True, "flag_set_by": "legacy"},
    "agent-flag": {**BASE, "flag": True, "flag_set_by": "agent"},
    "absent-setter-flag": {**BASE, "flag": True},
    "no-flag": dict(BASE),
}
#: Resume admits every fixture: the resume path reads no provenance.
RESUME_ADMITS = set(FIXTURES)
#: Open admits only an opening actor's flag.
OPEN_ADMITS = {"operator-flag", "legacy-flag"}
#: Positive controls the resume predicate must exclude.
CONTROLS = {
    "human_review": {**BASE, "phase": "human_review"},
    "hold": {**BASE, "status": "hold"},
}

# Conjunct grammar. Each returns a predicate: row -> True when the row is EXCLUDED.
_TOK = r"`?([A-Za-z_]+)`?"
GRAMMAR = (
    (re.compile(r"^(?:frontmatter )?`([A-Za-z_]+): ([^`]+)` absent$"),
     lambda k, v: (lambda r: _eq(r.get(k), v))),
    (re.compile(r"^(?:frontmatter )?`([A-Za-z_]+)` absent$"),
     lambda k: (lambda r: k in r)),
    (re.compile(r"^" + _TOK + r" not ((?:`[^`]+`/?)+)$"),
     lambda k, vs: (lambda r: any(_eq(r.get(k), v) for v in re.findall(r"`([^`]+)`", vs)))),
    (re.compile(r"^" + _TOK + r" (?:is|=) `([^`]+)`$"),
     lambda k, v: (lambda r: not _eq(r.get(k), v))),
)


def _eq(value, literal):
    if value is None:
        return False
    if isinstance(value, bool):
        value = "true" if value else "false"
    return str(value).strip() == literal.strip()


def fail(msg):
    print(f"resume-flag-clause FAILED: {msg}", file=sys.stderr)
    return 1


def gate_bullets(text):
    """-> (bullets, error). Top-level `- **label**…` bullets inside the gate section."""
    if text.count(GATE_START) != 1:
        return None, f"{GATE_FILE} carries the gate heading {text.count(GATE_START)} times, want 1"
    begin = text.index(GATE_START)
    end = text.find(GATE_END, begin)
    if end == -1:
        return None, f"{GATE_FILE} carries the gate heading but not {GATE_END!r}"
    out = []
    for line in text[begin:end].splitlines():
        m = re.match(r"^- \*\*(.+?)\*\*:?\s*(.*)$", line)
        if m:
            out.append((m.group(1).strip(), m.group(2).strip()))
    return out, None


def parse(label, body):
    """-> (exclusion predicates, error). Fail closed on any conjunct the grammar cannot read."""
    body = re.sub(r"\s*\([^()]*§[^()]*\)", "", body)  # a `(file § section)` pointer is not a predicate
    body = body.strip().rstrip(";.").strip()
    if not body:
        return None, f"`{label}` has no predicate"
    preds = []
    for conj in re.split(r",\s*(?:and\s+)?|\s+(?:and|AND)\s+", body):
        conj = conj.strip()
        for rx, build in GRAMMAR:
            m = rx.match(conj)
            if m:
                preds.append((conj, build(*m.groups())))
                break
        else:
            return None, f"`{label}` conjunct {conj!r} is not a readable predicate"
    return preds, None


def resume_predicate(text):
    bullets, err = gate_bullets(text)
    if err:
        return None, err
    parked = [b for b in bullets if b[0] == "not parked"]
    if len(parked) != 1:
        return None, f"the gate carries {len(parked)} `not parked` bullet(s), want 1"
    clauses = parked + [b for b in bullets if b[0] != "not parked" and VARYING.search(b[1] + " " + b[0])]
    preds = []
    for label, body in clauses:
        p, err = parse(label, body)
        if err:
            return None, err
        preds += [(label, c, f) for c, f in p]
    return preds, None


def excluded_by(preds, row):
    return [f"`{label}`: {conj}" for label, conj, f in preds if f(row)]


def open_selector(text):
    """-> (selector(tasks) -> admitted names, actors, error), from Step 0.5's own lines."""
    lines = {}
    for key in ("OPENING_ACTORS=", "cand=", "sel="):
        hits = [ln.strip() for ln in text.splitlines() if ln.strip().startswith(key)]
        if len(hits) != 1:
            return None, None, f"{OPEN_FILE} carries {len(hits)} `{key}` line(s), want 1"
        lines[key] = hits[0]
    actors = ast.literal_eval(lines["OPENING_ACTORS="].split("=", 1)[1])

    def select(tasks):
        env = {"tasks": tasks, "due": lambda t: True}
        exec("\n".join(lines.values()), {}, env)  # noqa: S102 — the repo's own selector lines
        return {t["name"] for t in env["sel"]}

    return select, tuple(actors), None


def resolver_actors(text):
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "OPENING_ACTORS" for t in node.targets
        ):
            return tuple(ast.literal_eval(node.value))
    return None


def main() -> int:
    for rel in (GATE_FILE, OPEN_FILE, RESOLVER, OWNER_DOC, *POINTER_CARRIERS):
        if not (REPO / rel).exists():
            return fail(f"{rel} does not exist")
    read = lambda rel: (REPO / rel).read_text(encoding="utf-8")  # noqa: E731

    preds, err = resume_predicate(read(GATE_FILE))
    if err:
        return fail(err)
    for name, row in FIXTURES.items():
        hits = excluded_by(preds, row)
        if (not hits) != (name in RESUME_ADMITS):
            return fail(
                f"resume gate excludes fixture {name!r} by {'; '.join(hits)} — the resume path "
                f"reads no flag and no provenance (operator rulings 2026-10-30, 2026-11-03)"
            )
    for name, row in CONTROLS.items():
        if not excluded_by(preds, row):
            return fail(f"resume gate admits control {name!r} — the `not parked` predicate lost its parked class")

    select, actors, err = open_selector(read(OPEN_FILE))
    if err:
        return fail(err)
    try:
        admitted = select([{**row, "name": name} for name, row in FIXTURES.items()])
    except Exception as exc:  # the selector lines no longer run as Python
        return fail(f"{OPEN_FILE} § Step 0.5 selector did not evaluate: {exc}")
    if admitted != OPEN_ADMITS:
        return fail(f"open selector admits {sorted(admitted)}, want {sorted(OPEN_ADMITS)}")
    if resolver_actors(read(RESOLVER)) != actors:
        return fail(f"{RESOLVER} OPENING_ACTORS disagrees with {OPEN_FILE}'s {actors}")

    heading = f"## {SECTION}"
    if sum(ln.strip() == heading for ln in read(OWNER_DOC).splitlines()) != 1:
        return fail(f"{OWNER_DOC} must carry `{heading}` exactly once")
    for rel in POINTER_CARRIERS:
        if f"§ {SECTION}" not in read(rel):
            return fail(f"{rel} does not name `{OWNER_DOC}` § {SECTION}")

    print(
        f"resume-flag-clause ok: resume admits {len(FIXTURES)}/{len(FIXTURES)} fixtures "
        f"and excludes {len(CONTROLS)}/{len(CONTROLS)} controls; open admits "
        f"{sorted(admitted)} via {actors}; {len(POINTER_CARRIERS)} surfaces name § {SECTION}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
