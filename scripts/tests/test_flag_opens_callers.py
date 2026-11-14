"""SC1: every path that decides "flag opens" resolves it through ONE predicate.

Two halves, and both are needed:

  * **Behavioural** — for each path, an operator-set flag is admitted and an agent-set or
    absent one is refused. This catches a path that still opens on any provenance.
  * **Structural** — each module must hold a reference to `flag_provenance`, the shared
    module. This catches the shape a behavioural test cannot see: a module that agrees
    with the shared predicate on today's fixtures while carrying its own copy of the rule
    (`flagged-rows.refusals()` did exactly that, and agreed with it on tasks).

⚠️ **`commands/open.md` is the fifth path, asserted here directly.** Its § Step 0.5
selector is executable prose an agent runs, not an importable module, so it cannot call a
Python function — but it does carry an `OPENING_ACTORS=` literal, and this file parses it
and compares it to the shared constant. `scripts/check-resume-flag-clause.py` guards the
same agreement from the command side; asserting it here too means a repoint of any of the
five paths turns THIS file red, which is what the positive control needs.

⚠️ **The positive control is run by hand at execution time:** repoint each path in turn and
watch this file go red — five repoints, five reds. A claim quantified over five paths is not
evidenced by one.

Run: python3 -m unittest discover -s scripts/tests -p 'test_flag_opens_callers.py'
"""

import importlib.util
import os
import re
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SCRIPTS, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Every importable open path, and the attribute that must point at the shared module.
CALLERS = (
    ("flagged_rows", "flagged-rows.py"),
    ("open_queue", "open-queue.py"),
    ("topic_spawn", "topic-spawn.py"),
    ("topic_keepalive", "topic-keepalive.py"),
)

#: The pages a path is asked about. `task` is strict, `goal`/`topic` tolerant, so each
#: kind gets the spelling it accepts.
ADMITTED = {"task": "flag: true\nflag_set_by: operator\n",
            "goal": "flag: 'true'\nflag_set_by: operator\n",
            "topic": "flag: on\nflag_set_by: operator\n"}
REFUSED = {"task": "flag: true\nflag_set_by: test-agent\n",
           "goal": "flag: 'true'\nflag_set_by: test-agent\n",
           "topic": "flag: on\nflag_set_by: test-agent\n"}
ABSENT = {"task": "flag: true\n", "goal": "flag: 'true'\n", "topic": "flag: on\n"}


class EveryPathReachesTheOnePredicate(unittest.TestCase):
    def test_each_caller_holds_a_reference_to_the_shared_module(self):
        """The structural half: a module that restates the rule fails here.

        Asserted on the module object, not on the source text — a `grep` for the name is
        satisfied by a comment, which is the false green the SC2 probe in
        `Development Guide` § *Run the change's own acceptance criterion too* warns about.
        """
        for name, filename in CALLERS:
            with self.subTest(caller=filename):
                module = _load(name, filename)
                self.assertTrue(
                    hasattr(module, "flag_provenance"),
                    "%s does not hold the shared predicate — it restates the rule" % filename)

    def test_the_shared_module_is_the_only_definition(self):
        shared = _load("flag_provenance", "flag_provenance.py")
        self.assertEqual(shared.OPENING_ACTORS, ("operator", "legacy"))

    def test_every_path_agrees_on_the_verdict(self):
        """The behavioural half, over all three kinds, through each module's own entry."""
        rows = _load("flagged_rows", "flagged-rows.py")
        queue = _load("open_queue", "open-queue.py")
        spawn = _load("topic_spawn", "topic-spawn.py")
        keepalive = _load("topic_keepalive", "topic-keepalive.py")
        shared = _load("flag_provenance", "flag_provenance.py")

        for kind in ("task", "goal", "topic"):
            with self.subTest(kind=kind):
                for block in (ADMITTED[kind], REFUSED[kind], ABSENT[kind]):
                    want = shared.flag_admits(block, kind)
                    self.assertEqual(rows.flag_admits(block, kind), want,
                                     "flagged-rows disagreed on %r" % block)
                    if kind == "task":
                        # `open-queue.is_flagged()` is the round's task admission.
                        self.assertEqual(queue.is_flagged(block), want,
                                         "open-queue disagreed on %r" % block)
                    if kind in ("goal", "topic"):
                        self.assertEqual(spawn.flagged(block, kind), want,
                                         "topic-spawn disagreed on %r" % block)
                        self.assertEqual(keepalive.flagged(block, kind), want,
                                         "topic-keepalive disagreed on %r" % block)

    def test_the_control_rows_are_not_vacuous(self):
        """Guard against a table where every row refuses — that would pass the loop above
        while proving nothing."""
        shared = _load("flag_provenance", "flag_provenance.py")
        admitted = {k: shared.flag_admits(v, k) for k, v in ADMITTED.items()}
        refused = {k: shared.flag_admits(v, k) for k, v in REFUSED.items()}
        absent = {k: shared.flag_admits(v, k) for k, v in ABSENT.items()}
        self.assertEqual(admitted, {"task": True, "goal": True, "topic": True})
        self.assertEqual(refused, {"task": False, "goal": False, "topic": False})
        self.assertEqual(absent, {"task": False, "goal": False, "topic": False})

    def test_a_legacy_row_opens_at_all_three_tiers(self):
        """⚠️ 270 rows carry `flag_set_by: legacy` after the 2026-09-30 disposition.

        A build admitting `operator` alone passes every other criterion in this file while
        all of them stop opening, so the third actor needs its own assertion at every tier.
        """
        shared = _load("flag_provenance", "flag_provenance.py")
        for kind, body in (("task", "flag: true"), ("goal", "flag: 'true'"), ("topic", "flag: on")):
            with self.subTest(kind=kind):
                self.assertTrue(shared.flag_admits(body + "\nflag_set_by: legacy\n", kind))

    def test_the_goal_and_topic_widening_to_one_is_pinned(self):
        """⚠️ `FLAG_TRUTHY` admits `1`, which a YAML loader reads as an **int**, so a goal
        or topic page saying `flag: 1` opens while a task page does not (`YAML_TRUE` has no
        `1`, matching `commands/open.md` § Step 0.5's `t.get('flag') is True`).

        Nothing else exercises this, so a later "tidy" of either tuple would silently widen
        or narrow the three tiers without a red test.
        """
        shared = _load("flag_provenance", "flag_provenance.py")
        page = "flag: 1\nflag_set_by: operator\n"
        self.assertTrue(shared.flag_admits(page, "goal"))
        self.assertTrue(shared.flag_admits(page, "topic"))
        self.assertFalse(shared.flag_admits(page, "task"))

    def test_the_task_batch_selector_matches_the_shared_constant(self):
        """`commands/open.md` § Step 0.5 — the fifth path, an executable-prose selector.

        Parsed rather than imported: the command file's `OPENING_ACTORS=` literal is what
        an agent actually runs, so that literal is the thing that must agree.
        """
        shared = _load("flag_provenance", "flag_provenance.py")
        path = os.path.join(os.path.dirname(_SCRIPTS), "commands", "open.md")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        match = re.search(r"^OPENING_ACTORS=\((.*)\)$", text, re.MULTILINE)
        self.assertIsNotNone(match, "commands/open.md carries no OPENING_ACTORS= selector")
        parsed = tuple(part.strip().strip("'\"") for part in match.group(1).split(","))
        self.assertEqual(parsed, shared.OPENING_ACTORS)


if __name__ == "__main__":
    unittest.main()
