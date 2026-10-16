#!/usr/bin/env python3
"""SC2's behavioural probe — what a manager tick may raise over a fixture subject.

⚠️ **Not a grep over the rule text.** A grep over `commands/manager-loop.md` passes on
the day the rule is deleted from the arm a manager actually reads, and passes on a tree
where nothing enforces it. This probe runs the *decision path* an `AskUserQuestion`
takes — `scripts/manager-menu-guard.py`, invoked as a subprocess exactly as
`hooks/hooks.json` invokes it on `PreToolUse` — over a fixture subject built on disk,
and asserts on the exit code AND on the record the run leaves behind.

⚠️ **Why the decision path and not a live model turn.** A manager tick is a model turn:
not reproducible on demand, and it would run against the real fleet. The enforcement half
is deterministic, and it is what makes the criterion observable — the guard's record is
the artifact SC4 reads. A probe built on a live turn could never be shown capable of
going red, which is the property `scripts/tests/manager_menu_probe_fixture.py` exists to
establish.

What green means, per row of the fixture's tracked set:

  - an approved row at `phase: planning` with no live session is the manager's own work
    to open, so a menu offering to open it must be **DENIED**, and the deny **RECORDED**
  - a row at `phase: todo` is the operator's decision, so a menu offering to approve it
    must be **ALLOWED** — the operator-only channel stays open, which is what keeps a
    deny-everything guard from satisfying this probe
  - the same manager-owned menu from a non-manager session must be **ALLOWED** — this
    hook's scope is managers, and the scope control is what keeps the deny from reading
    as "the guard denies menus"

⚠️ **The fixture's tracked set is READ, never decorated.** Each row's own frontmatter
decides which side of the line it falls on, so a fixture that quietly stopped carrying an
approved row fails here instead of passing on an empty matrix — the failure mode
`plan-task` names as "a name is not a producer", one level down.

Run:  python3 scripts/manager-menu-probe.py [--fixture-root DIR]
Exit 0 when every case behaves; exit 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUARD = HERE / "manager-menu-guard.py"

# The fixture's session ids. Deliberately not UUID-shaped and not any live id: a probe
# that borrowed a real session id would classify against the real registry.
MANAGER_SESSION = "fixture-manager-0000"
WORKER_SESSION = "fixture-worker-0001"

# The topic page the manager's name resolves through — rule 1's name arm. The session
# registry label carries the ` Manager` suffix the guard strips.
TOPIC_TITLE = "Sentry Agent"
MANAGER_NAME = f"{TOPIC_TITLE} Manager"

# The fixture's tracked set. ⚠️ **Both titles are deliberately BARE** — neither carries a
# manager-owned object noun (`row` / `task` / `page` / `box` / `goal`), because that is
# what a real task title looks like. A fixture whose rows happened to be named
# "… Approved Row" would let the object-noun half of the predicate answer every case and
# never exercise the title half at all.
APPROVED_ROW = "Sentry Agent - Retire the Legacy Gate"
TODO_ROW = "Sentry Agent - Adopt the New Menu Rule"


def frontmatter(path: Path) -> dict:
    """The leading `---` block as a flat mapping. Tolerant: an unparsable block is `{}`."""
    out: dict = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    if not lines or lines[0].strip() != "---":
        return out
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def build_fixture(root: Path) -> dict:
    """Build a throwaway vault + registry + state dir, and return its description.

    Everything the guard reads is redirected here — `OBSIDIAN_DIR`, `CLAUDE_SESSIONS_DIR`,
    `CLAUDE_PROJECTS_DIR`, `SUPERVISOR_MANAGER_MENU_GUARD_STATE_DIR` — so no case can
    reach the live fleet, the live registry, or the live record store.
    """
    vault = root / "obsidian" / "fixture-vault"
    (vault / "23 Topics").mkdir(parents=True, exist_ok=True)
    (vault / "25 Tasks").mkdir(parents=True, exist_ok=True)
    (vault / "23 Topics" / f"{TOPIC_TITLE}.md").write_text(
        "---\npage_type: topic\n---\n\nFixture topic page — exists so rule 1's name arm "
        "resolves.\n",
        encoding="utf-8",
    )

    tracked = root / "tracked"
    tracked.mkdir(parents=True, exist_ok=True)
    # ⚠️ `claude_session_id` is left EMPTY on the approved row: that is the criterion's
    # "no live session" half, and it is what makes opening the row the manager's own work
    # rather than a collision.
    (tracked / f"{APPROVED_ROW}.md").write_text(
        "---\nstatus: in_progress\nphase: planning\nclaude_session_id: \n---\n\n"
        "Fixture row: approved, planned, no live session.\n",
        encoding="utf-8",
    )
    (tracked / f"{TODO_ROW}.md").write_text(
        "---\nstatus: next\nphase: todo\nclaude_session_id: \n---\n\n"
        "Fixture row: waiting in the operator's approval inbox.\n",
        encoding="utf-8",
    )

    sessions = root / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    for session_id, name in ((MANAGER_SESSION, MANAGER_NAME),
                             (WORKER_SESSION, "A Worker Session")):
        (sessions / f"{session_id}.json").write_text(
            json.dumps({"sessionId": session_id, "name": name, "cwd": str(root)}),
            encoding="utf-8",
        )

    projects = root / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)

    return {
        "root": root,
        "vault": vault,
        "tracked": tracked,
        "sessions": sessions,
        "projects": projects,
        "state": state,
    }


def guard_env(fixture: dict) -> dict:
    env = dict(os.environ)
    env["OBSIDIAN_DIR"] = str(fixture["root"] / "obsidian")
    env["CLAUDE_SESSIONS_DIR"] = str(fixture["sessions"])
    env["CLAUDE_PROJECTS_DIR"] = str(fixture["projects"])
    env["SUPERVISOR_MANAGER_MENU_GUARD_STATE_DIR"] = str(fixture["state"])
    env.pop("SUPERVISOR_MANAGER_MENU_GUARD", None)
    return env


def run_guard(payload: dict, env: dict) -> tuple[int, str]:
    """Invoke the guard exactly as the hook does: JSON on stdin, verdict as exit code."""
    result = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    return result.returncode, result.stdout + result.stderr


def last_record(fixture: dict, session_id: str) -> dict | None:
    """The newest record the guard wrote for `session_id`, or `None`.

    ⚠️ Read from the fixture's own state dir at the path the guard documents — a probe
    that read the verdict without reading the record could not tell a deny from a guard
    that never ran, which is the whole reason the record exists.
    """
    path = fixture["state"] / f"{session_id}.jsonl"
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return None
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except ValueError:
        return None


def menu(question: str, labels: list[str]) -> dict:
    return {
        "questions": [
            {"question": question,
             "header": "Decision",
             "options": [{"label": label, "description": ""} for label in labels],
             "multiSelect": False}
        ]
    }


def cases(rows: dict) -> list[dict]:
    """The tick's candidate menus, derived from the fixture's own tracked set."""
    return [
        {
            "id": "manager-owned / object-noun form",
            "session": MANAGER_SESSION,
            "tool_input": menu(f"Open {rows['approved']}?",
                               ["Open the approved row", "Leave it closed"]),
            "expect_exit": 2,
            "expect_reason": "manager-owned-action",
            "expect_verbs": ["open"],
        },
        {
            "id": "manager-owned / bare task title",
            "session": MANAGER_SESSION,
            "tool_input": menu(f"Resume {rows['approved']}?",
                               [f'Resume "{rows["approved"]}"', "Wait for the operator"]),
            "expect_exit": 2,
            "expect_reason": "manager-owned-action",
            "expect_verbs": ["resume"],
        },
        {
            "id": "operator-only / phase: todo approval",
            "session": MANAGER_SESSION,
            "tool_input": menu(f"Approve {rows['todo']}?",
                               [f'Approve "{rows["todo"]}"', "Leave it unchanged"]),
            "expect_exit": 0,
            "expect_reason": "no-manager-action",
            "expect_verbs": [],
        },
        {
            "id": "operator-only / production-touching",
            "session": MANAGER_SESSION,
            "tool_input": menu("Deploy now?",
                               ["Deploy to prod now", "Keep it unchanged"]),
            "expect_exit": 0,
            "expect_reason": "no-manager-action",
            "expect_verbs": [],
        },
        {
            "id": "scope control / non-manager session",
            "session": WORKER_SESSION,
            "tool_input": menu("Open the approved row?",
                               ["Open the approved row", "Leave it closed"]),
            "expect_exit": 0,
            "expect_reason": "non-manager",
            "expect_verbs": ["open"],
        },
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture-root",
                        help="use this ALREADY-BUILT fixture root as-is. Never rebuilds: "
                             "the falsification fixture mutates a tree and re-runs, and a "
                             "probe that re-created it would overwrite every mutation and "
                             "report a green that means nothing.")
    args = parser.parse_args(argv)

    owned_tmp = None
    if args.fixture_root:
        root = Path(args.fixture_root)
        if not (root / "tracked").is_dir():
            print(f"REFUSED — {root} is not a built fixture (no tracked/ dir). "
                  "Omit --fixture-root to build a temporary one.")
            return 2
        fixture = {
            "root": root,
            "vault": root / "obsidian" / "fixture-vault",
            "tracked": root / "tracked",
            "sessions": root / "sessions",
            "projects": root / "projects",
            "state": root / "state",
        }
    else:
        owned_tmp = tempfile.TemporaryDirectory(prefix="manager-menu-probe-")
        fixture = build_fixture(Path(owned_tmp.name))

    try:
        # ⚠️ The tracked set is read, and the read is ASSERTED. An empty or mis-shaped
        # fixture would otherwise run zero meaningful cases and exit 0.
        approved = frontmatter(fixture["tracked"] / f"{APPROVED_ROW}.md")
        todo = frontmatter(fixture["tracked"] / f"{TODO_ROW}.md")
        fixture_faults = []
        if (approved.get("phase") or "") != "planning":
            fixture_faults.append(f"approved row phase is {approved.get('phase')!r}, not 'planning'")
        if (approved.get("claude_session_id") or "").strip():
            fixture_faults.append("approved row carries a live claude_session_id")
        if (todo.get("phase") or "") != "todo":
            fixture_faults.append(f"todo row phase is {todo.get('phase')!r}, not 'todo'")

        env = guard_env(fixture)
        failures: list[str] = []

        print(f"fixture: {fixture['root']}")
        print(f"manager: {MANAGER_NAME!r} ({MANAGER_SESSION})  "
              f"non-manager: {WORKER_SESSION}")
        print(f"tracked: approved {APPROVED_ROW!r} (phase: planning, no session) · "
              f"todo {TODO_ROW!r}")
        print()

        for fault in fixture_faults:
            print(f"FAIL fixture                             -> {fault}")
            failures.append(f"fixture: {fault}")

        rows = {"approved": APPROVED_ROW, "todo": TODO_ROW}
        for case in cases(rows):
            payload = {"session_id": case["session"],
                       "tool_name": "AskUserQuestion",
                       "tool_input": case["tool_input"]}
            code, _ = run_guard(payload, env)
            record = last_record(fixture, case["session"])

            problems = []
            if code != case["expect_exit"]:
                problems.append(f"exit {code}, expected {case['expect_exit']}")
            if record is None:
                problems.append("no record written")
            else:
                if record.get("reason") != case["expect_reason"]:
                    problems.append(f"reason {record.get('reason')!r}, "
                                    f"expected {case['expect_reason']!r}")
                if list(record.get("verbs") or []) != case["expect_verbs"]:
                    problems.append(f"verbs {record.get('verbs')!r}, "
                                    f"expected {case['expect_verbs']!r}")
                if case["expect_exit"] and record.get("decision") != "deny":
                    problems.append(f"decision {record.get('decision')!r}, expected 'deny'")

            if problems:
                print(f"FAIL {case['id']:38} -> {'; '.join(problems)}")
                failures.append(f"{case['id']}: {'; '.join(problems)}")
            else:
                verdict = "deny" if code else "allow"
                print(f"OK   {case['id']:38} -> {verdict} "
                      f"({case['expect_reason']})")

        print()
        if failures:
            print(f"FAILED — {len(failures)} case(s):")
            for failure in failures:
                print(f"  - {failure}")
            return 1
        print(f"PASSED — fixture + {len(cases(rows))} cases: "
              "no manager-owned menu reaches the operator")
        return 0
    finally:
        if owned_tmp is not None:
            owned_tmp.cleanup()


if __name__ == "__main__":
    sys.exit(main())
