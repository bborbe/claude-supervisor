#!/usr/bin/env python3
"""Falsification fixture for `manager-menu-probe.py`.

A probe that has never been red is not known to be capable of red. The probe's green says
"no manager-owned menu reaches the operator over this fixture" — and a probe that reported
green unconditionally would say exactly the same thing. This fixture establishes the
difference: it builds the probe's own fixture, MUTATES it so the claim stops holding, and
asserts the probe goes red naming the case that broke.

The positive control comes first: an unmutated fixture must pass. Without it, a probe that
failed on everything would satisfy every red case below.

⚠️ **Each case names the failures it EXPECTS and the cases that must stay green.** A red
that fires on everything is not a red — it is a broken probe, and the isolation half is
what separates the two. `manager unresolvable` must break exactly the two manager-owned
cases and leave the operator-only ones and the scope control untouched; a mutation that
also broke those would be evidence the probe keys off something other than the manager
verdict it claims to read.

⚠️ **The mutations are applied to the FIXTURE, never to the guard.** Editing the subject
under test would prove only that a broken guard is broken. What these cases establish is
that the probe's green depends on the fixture really carrying what the criterion says —
an approved `phase: planning` row with no live session, a session that resolves to a
manager, and a record store that accepts a write.

Run:  python3 scripts/tests/manager_menu_probe_fixture.py
Exit 0 when every case behaves; exit 1 otherwise.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROBE = HERE.parent / "manager-menu-probe.py"

OPERATOR_ONLY = ("operator-only / phase: todo approval",
                 "operator-only / production-touching")
SCOPE_CONTROL = "scope control / non-manager session"

# ⚠️ Substrings, not the whole ids: the probe pads its id column to a fixed width, so an
# exact match on a trailing space would silently stop matching if that width ever changed.
MANAGER_OWNED_IDS = ("manager-owned / object-noun form", "manager-owned / bare task title")


def load_probe():
    spec = importlib.util.spec_from_file_location("manager_menu_probe", PROBE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_probe(root: Path) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, str(PROBE), "--fixture-root", str(root)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    return result.returncode, result.stdout + result.stderr


def failed_cases(out: str) -> list[str]:
    """Every `FAIL <case>` line's case id, in report order. The fixture-fault line is
    excluded — it has no case id and is asserted separately."""
    found = []
    for line in out.splitlines():
        if line.startswith("FAIL ") and not line.startswith("FAIL fixture"):
            found.append(line[5:].split("->")[0].strip())
    return found


# --- the mutations ------------------------------------------------------------
#
# Each takes the built fixture root and breaks exactly one of the things the probe's
# green depends on.


def mutate_manager_unresolvable(probe, root: Path) -> None:
    """Rename the topic page so rule 1's name arm stops resolving the manager.

    The manager's registry label is `<Topic> Manager`, and the guard strips the suffix
    and looks the remainder up among the vault's page titles. Remove the page and the
    session is no longer a manager, so its manager-owned menus are ALLOWED — which is
    precisely the state the criterion forbids, and the probe must say so.
    """
    page = root / "obsidian" / "fixture-vault" / "23 Topics" / f"{probe.TOPIC_TITLE}.md"
    page.rename(page.with_name("Some Other Topic.md"))


def mutate_record_store_unwritable(probe, root: Path) -> None:
    """Replace the record store with a regular file, so no record can be written.

    The guard's verdict is unchanged — it still exits 2 — so a probe that read only the
    exit code would stay green. This case is what pins the probe to the RECORD, which is
    the artifact SC4 reads and the only thing that separates "the guard denied" from
    "the guard never ran".
    """
    state = root / "state"
    if state.is_dir():
        for child in state.iterdir():
            child.unlink()
        state.rmdir()
    state.write_text("not a directory\n", encoding="utf-8")


def mutate_tracked_set_loses_approved_row(probe, root: Path) -> None:
    """Advance the approved row past `phase: planning`.

    The criterion is scoped to a row that is approved, planned and has no live session.
    A fixture that quietly stopped carrying one would leave the probe running its cases
    against a subject the criterion does not describe, so the probe asserts the fixture
    itself before it asserts any verdict.
    """
    row = root / "tracked" / f"{probe.APPROVED_ROW}.md"
    row.write_text(
        row.read_text(encoding="utf-8").replace("phase: planning", "phase: execution"),
        encoding="utf-8",
    )


CASES = (
    {
        "id": "manager unresolvable",
        "mutate": mutate_manager_unresolvable,
        "expect_fail": MANAGER_OWNED_IDS,
        "expect_green": OPERATOR_ONLY + (SCOPE_CONTROL,),
    },
    {
        "id": "record store unwritable",
        "mutate": mutate_record_store_unwritable,
        "expect_fail": MANAGER_OWNED_IDS,
        "expect_green": (),
        "expect_substring": "no record written",
    },
    {
        "id": "tracked set loses the approved row",
        "mutate": mutate_tracked_set_loses_approved_row,
        "expect_fail": (),
        "expect_green": OPERATOR_ONLY + (SCOPE_CONTROL,) + MANAGER_OWNED_IDS,
        "expect_substring": "approved row phase is 'execution'",
    },
)


def main() -> int:
    probe = load_probe()
    failures: list[str] = []

    with tempfile.TemporaryDirectory(prefix="manager-menu-probe-fixture-") as tmp:
        root = Path(tmp)

        # ---- positive control: unmutated fixture must pass --------------------
        probe.build_fixture(root / "control")
        code, out = run_probe(root / "control")
        if code == 0:
            print("OK   control (unmutated)                  -> exit 0")
        else:
            print(f"FAIL control (unmutated)                  -> exit {code}")
            failures.append("control: unmutated fixture did not pass")
            for line in out.splitlines():
                if line.startswith("FAIL"):
                    print(f"       {line}")

        # ---- one red case per mutation ---------------------------------------
        for index, case in enumerate(CASES):
            case_root = root / f"case-{index}"
            probe.build_fixture(case_root)
            case["mutate"](probe, case_root)

            code, out = run_probe(case_root)
            broke = failed_cases(out)
            problems: list[str] = []

            if code == 0:
                problems.append("probe stayed green")
            for expected in case["expect_fail"]:
                if not any(expected in name for name in broke):
                    problems.append(f"did not name {expected!r}")
            for green in case["expect_green"]:
                if any(green in name for name in broke):
                    problems.append(f"also broke {green!r} (mutation not isolated)")
            substring = case.get("expect_substring")
            if substring and substring not in out:
                problems.append(f"report does not carry {substring!r}")

            if problems:
                print(f"FAIL {case['id']:38} -> {'; '.join(problems)}")
                failures.append(f"{case['id']}: {'; '.join(problems)}")
            else:
                print(f"OK   {case['id']:38} -> exit {code}, "
                      f"{len(broke)} case(s) named")

    print()
    if failures:
        print(f"FAILED — {len(failures)} case(s):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"PASSED — control + {len(CASES)} red cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
