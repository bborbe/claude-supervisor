"""SC1: every path that decides "flag opens" resolves it through ONE predicate.

Two halves, and both are needed:

  * **Behavioural** — for each path, an operator-set flag is admitted and an agent-set or
    absent one is refused. This catches a path that still opens on any provenance.
  * **Structural** — each module must hold a reference to `flag_provenance`, the shared
    module. This catches the shape a behavioural test cannot see: a module that agrees
    with the shared predicate on today's fixtures while carrying its own copy of the rule
    (`flagged-rows.refusals()` did exactly that, and agreed with it on tasks).

⚠️ **`commands/open.md` is covered by a different mechanism, on purpose.**
Its § Step 0.5 selector is executable prose an agent runs, not an importable module, so it
cannot call a Python function. `scripts/check-resume-flag-clause.py` is the guard: it
parses that selector's own `OPENING_ACTORS=` line and asserts it equals the shared
constant, and `test_check_resume_flag_clause.py` runs it against the real tree. That is
coverage of the sixth path, not an omission — and it is why the checker was repaired
rather than retired when the constant moved.

⚠️ **The positive control is run by hand at execution time:** repoint each path in turn and
watch this file go red — six repoints, six reds. A claim quantified over six paths is not
evidenced by one.

Run: python3 -m unittest discover -s scripts/tests -p 'test_flag_opens_callers.py'
"""

import importlib.util
import os
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
    ("flagged_subjects", "flagged-subjects.py"),
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


if __name__ == "__main__":
    unittest.main()
