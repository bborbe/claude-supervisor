#!/usr/bin/env python3
"""Tests for scripts/session-scope.py.

The module answers one question — does this session's page belong to the asking manager's
goal? — and every case here pins a way it could answer wrongly. The dangerous direction is
always the same: excluding a gate the manager EXISTS to see, or excluding on a read that
failed. Each test therefore carries a positive and a negative control where the distinction
matters.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import os
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "session-scope.py")

_spec = importlib.util.spec_from_file_location("session_scope", _SCRIPT)
ss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ss)

ME = "49b471f1-a8a6-48b0-b2b2-14b9cb2437e3"
OWN_TASK_SESSION = "aaaaaaaa-1111-4111-8111-111111111111"
FOREIGN_SESSION = "bbbbbbbb-2222-4222-8222-222222222222"
NO_GOAL_SESSION = "cccccccc-3333-4333-8333-333333333333"
METRICS_SESSION = "dddddddd-4444-4444-8444-444444444444"
SECOND_PAGE_SESSION = "eeeeeeee-5555-4555-8555-555555555555"
PROSE_SESSION = "ffffffff-6666-4666-8666-666666666666"
LAST_KEY_FOREIGN = "12121212-1212-4212-8212-121212121212"
LAST_KEY_OWN = "13131313-1313-4313-8313-131313131313"

SELF_GOAL = "Managers Unblock Their Own Workers and Approve Routine Permissions Within Policy"


def _page(front, body="\n# Impact\n\nstub\n"):
    return "---\n" + front + "page_type: task\n---\nTags: [[Task]]\n\n---\n" + body


class Vault:
    """A throwaway vault with the `25 Tasks` / `24 Goals` layout the resolver probes."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        os.makedirs(os.path.join(self.root, "25 Tasks"))
        os.makedirs(os.path.join(self.root, "24 Goals"))

    def task(self, name, front, body="\n# Impact\n\nstub\n"):
        with open(os.path.join(self.root, "25 Tasks", name + ".md"), "w", encoding="utf-8") as handle:
            handle.write(_page(front, body))
        return self

    def task_ending_at(self, name, front, body="\n# Impact\n\nstub\n"):
        """A task whose frontmatter ENDS at `front` — no `page_type:` trailer.

        `task()` appends `page_type: task`, so a `goals:` block written through it is never
        the LAST key in the frontmatter. That trailer is exactly what hid the last-key bug:
        every page it builds has a key after the block under test. This writer removes it.
        """
        with open(os.path.join(self.root, "25 Tasks", name + ".md"), "w", encoding="utf-8") as handle:
            handle.write("---\n" + front + "\n---\nTags: [[Task]]\n\n---\n" + body)
        return self

    def goal(self, name, front, links):
        body = "\n# Impact\n\nstub\n\n# Tasks\n\n" + "".join(
            f"- [ ] [[{link}]] (→ SC1)\n" for link in links
        ) + "\n# Progress\n\nnothing\n"
        with open(os.path.join(self.root, "24 Goals", name + ".md"), "w", encoding="utf-8") as handle:
            handle.write(_page(front, body))
        return self

    def close(self):
        self._tmp.cleanup()


class Scope(unittest.TestCase):
    def setUp(self):
        self.vault = Vault()
        self.vault.goal(SELF_GOAL, f"manager_session_id: {ME}\n", ["Own Task"])
        self.vault.task("Own Task", f"claude_session_id: {OWN_TASK_SESSION}\n")
        self.vault.task(
            "Foreign Task",
            f"claude_session_id: {FOREIGN_SESSION}\n"
            "goals:\n    - '[[Some Other Goal]]'\n",
        )
        self.vault.task("No Goal Task", f"claude_session_id: {NO_GOAL_SESSION}\n")
        self.vault.task(
            "Metrics Task",
            "claude_session_id: 99999999-9999-4999-8999-999999999999\n"
            "goals:\n    - '[[Third Goal]]'\n"
            "metrics_sessions:\n"
            f"    - session_id: {METRICS_SESSION}\n",
        )
        self.vault.task(
            "Declares The Goal But Is Unlisted",
            "claude_session_id: 88888888-8888-4888-8888-888888888888\n"
            f"goals:\n    - '[[{SELF_GOAL}]]'\n",
        )
        self.scope = ss.load(ME, [self.vault.root])

    def tearDown(self):
        self.vault.close()

    def test_a_goal_page_manager_session_scopes_to_its_tasks_list(self):
        """An operator-opened manager is never spawned, so this stamp is its only identity."""
        self.assertTrue(self.scope.resolved, self.scope.reason)
        self.assertEqual(self.scope.self_goals, [SELF_GOAL])

    def test_an_own_task_is_not_excluded(self):
        self.assertEqual(self.scope.exclusion(OWN_TASK_SESSION, ME), (False, ""))

    def test_a_foreign_task_names_its_goal(self):
        self.assertEqual(
            self.scope.exclusion(FOREIGN_SESSION, ME),
            (True, "foreign-goal:Some Other Goal"),
        )

    def test_a_task_declaring_no_goal_is_owner_unknown(self):
        """The exclusion that names nobody, and still an exclusion by contract."""
        self.assertEqual(self.scope.exclusion(NO_GOAL_SESSION, ME), (True, "owner-unknown"))

    def test_a_session_stamped_only_through_a_metrics_entry_resolves(self):
        """Measured case pane 260: `claude_session_id` alone would leave it unattributed."""
        self.assertEqual(
            self.scope.exclusion(METRICS_SESSION, ME),
            (True, "foreign-goal:Third Goal"),
        )

    def test_a_task_declaring_the_goal_is_in_scope_even_when_the_goal_page_omits_it(self):
        """The widening arm: under-inclusion excludes an own gate, the dangerous direction."""
        self.assertEqual(self.scope.exclusion("88888888-8888-4888-8888-888888888888", ME), (False, ""))

    def test_the_asking_session_is_never_excluded(self):
        self.assertEqual(self.scope.exclusion(ME, ME), (False, ""))

    def test_a_session_with_no_page_is_not_excluded(self):
        """Unattributed is not foreign — the fail-open direction every hop takes."""
        self.assertEqual(self.scope.exclusion("11111111-1111-4111-8111-111111111111", ME), (False, ""))

    def test_a_session_on_two_pages_is_not_excluded(self):
        """An ambiguous stamp is not a foreign one; excluding it would guess."""
        self.vault.task("Second Claim", f"claude_session_id: {SECOND_PAGE_SESSION}\n")
        self.vault.task(
            "Second Claim Two",
            f"claude_session_id: {SECOND_PAGE_SESSION}\n"
            "goals:\n    - '[[Some Other Goal]]'\n",
        )
        scope = ss.load(ME, [self.vault.root])
        self.assertEqual(scope.exclusion(SECOND_PAGE_SESSION, ME), (False, ""))

    def test_a_stamp_quoted_in_progress_prose_is_not_read(self):
        """Frontmatter only: a `- session_id:` line in prose must never attribute a session."""
        self.vault.task(
            "Prose Quoter",
            "claude_session_id: 77777777-7777-4777-8777-777777777777\n",
            body=f"\n# Progress\n\n- session_id: {PROSE_SESSION}\n",
        )
        scope = ss.load(ME, [self.vault.root])
        self.assertEqual(scope.exclusion(PROSE_SESSION, ME), (False, ""))


class GoalsBlockAsLastFrontmatterKey(unittest.TestCase):
    """A `goals:` block that is the LAST key in the frontmatter.

    `_frontmatter()` returns `text[3:end]`, so a block that ends the frontmatter has no
    trailing newline. `_GOALS_BLOCK` required every list item to end `\\n`, so such a block
    matched ZERO items — silently, with no error — and `_goals()` returned `[]`.

    Two faces, and the second is the dangerous one:

    - a foreign page loses its owner, so the exclusion names nobody (`owner-unknown`) instead
      of the goal it actually belongs to — the "it can never name the owner" defect this
      whole change exists to remove, restored for that page shape;
    - the asking manager's own goal stops widening its scope, so its own workers' gates are
      excluded from the feed it exists to see — the fleet-silence direction the module says
      it fails open to avoid.

    Every earlier case missed both because `Vault.task()` appends `page_type: task`, which
    always puts a key AFTER the block under test.
    """

    def test_goals_reads_a_block_with_no_key_after_it(self):
        """The mechanism, pinned at the line the defect lives on."""
        front = "\nclaude_session_id: %s\ngoals:\n    - '[[Some Other Goal]]'" % ME
        self.assertEqual(ss._goals(front), ["Some Other Goal"])

    def test_a_foreign_page_whose_goals_block_is_last_still_names_its_goal(self):
        vault = Vault()
        try:
            vault.goal(SELF_GOAL, f"manager_session_id: {ME}\n", ["Own Task"])
            vault.task("Own Task", f"claude_session_id: {OWN_TASK_SESSION}\n")
            vault.task_ending_at(
                "Last Key Foreign",
                f"claude_session_id: {LAST_KEY_FOREIGN}\ngoals:\n    - '[[Some Other Goal]]'",
            )
            scope = ss.load(ME, [vault.root])
            self.assertEqual(
                scope.exclusion(LAST_KEY_FOREIGN, ME),
                (True, "foreign-goal:Some Other Goal"),
            )
        finally:
            vault.close()

    def test_a_task_page_manager_whose_goals_block_is_last_still_scopes_its_goal(self):
        """The dangerous direction: an own gate excluded because the goal never widened."""
        vault = Vault()
        try:
            vault.goal(
                "Some Goal",
                "manager_session_id: 12341234-1234-4123-8123-123412341234\n",
                ["Manager Task", "Own Task"],
            )
            vault.task_ending_at(
                "Manager Task",
                f"claude_session_id: {ME}\ngoals:\n    - '[[Some Goal]]'",
            )
            vault.task("Own Task", f"claude_session_id: {LAST_KEY_OWN}\n")
            scope = ss.load(ME, [vault.root])
            self.assertEqual(scope.self_goals, ["Some Goal"])
            self.assertEqual(scope.exclusion(LAST_KEY_OWN, ME), (False, ""))
        finally:
            vault.close()


class Unresolved(unittest.TestCase):
    """A scope that cannot be read must exclude NOTHING — never "nothing is mine"."""

    def setUp(self):
        self.vault = Vault()
        self.vault.goal("Some Goal", "manager_session_id: 12341234-1234-4123-8123-123412341234\n", [])
        self.vault.task("A Task", f"claude_session_id: {FOREIGN_SESSION}\n"
                                  "goals:\n    - '[[Some Goal]]'\n")

    def tearDown(self):
        self.vault.close()

    def test_a_manager_with_no_page_is_unresolved_and_excludes_nothing(self):
        scope = ss.load(ME, [self.vault.root])
        self.assertFalse(scope.resolved)
        self.assertIn("stamped on no task or goal page", scope.reason)
        self.assertEqual(scope.exclusion(FOREIGN_SESSION, ME), (False, ""))

    def test_a_missing_vault_path_is_a_reason_not_an_exception(self):
        scope = ss.load(ME, [os.path.join(self.vault.root, "nope")])
        self.assertFalse(scope.resolved)
        self.assertIn("does not exist", scope.reason)

    def test_a_goal_with_no_tasks_scopes_to_itself_alone(self):
        """A manager with no workers still has a scope — its own goal — and everything
        attributable outside it is foreign. Unresolved is a MISSING page, not an empty one."""
        vault = Vault()
        try:
            vault.goal("Lonely Goal", "manager_session_id: 12341234-1234-4123-8123-123412341234\n", [])
            vault.task("Elsewhere", f"claude_session_id: {FOREIGN_SESSION}\n"
                                    "goals:\n    - '[[Another Goal]]'\n")
            scope = ss.load("12341234-1234-4123-8123-123412341234", [vault.root])
            self.assertTrue(scope.resolved)
            self.assertEqual(scope.own_titles, {"Lonely Goal"})
            self.assertEqual(
                scope.exclusion(FOREIGN_SESSION, "12341234-1234-4123-8123-123412341234"),
                (True, "foreign-goal:Another Goal"),
            )
        finally:
            vault.close()

    def test_no_self_id_is_unresolved(self):
        scope = ss.load(None, [self.vault.root])
        self.assertFalse(scope.resolved)
        self.assertIn("no --self", scope.reason)


class DuplicateGoalBasename(unittest.TestCase):
    """Two vaults carrying a goal page with the same basename must not fight.

    `goals` is keyed by page basename, and the `private-*` / `seibert-*` families routinely
    reuse one (`24 Goals/…` appears in every vault). Overwriting made the LAST vault's
    `# Tasks` the only input `own_titles` was built from, so an own task linked only from
    the first vault's page was EXCLUDEd as foreign -- the fleet-silence direction the
    module names as the dangerous one.
    """

    def setUp(self):
        self.first = Vault()
        self.second = Vault()

    def tearDown(self):
        self.first.close()
        self.second.close()

    def test_a_second_page_with_the_same_basename_merges_rather_than_replaces(self):
        self.first.goal(SELF_GOAL, f"manager_session_id: {ME}\n", ["Own Task"])
        self.first.task("Own Task", f"claude_session_id: {OWN_TASK_SESSION}\n")
        self.second.goal(SELF_GOAL, "", ["Some Other Task"])

        scope = ss.load(ME, [self.first.root, self.second.root])

        # Asserted as the UNION so the pin does not depend on which vault the walk reads
        # first: a replace fails this in either order.
        self.assertEqual(scope.goals[SELF_GOAL], {"Own Task", "Some Other Task"})
        self.assertEqual(scope.exclusion(OWN_TASK_SESSION, ME), (False, ""))

    def test_an_empty_second_page_does_not_wipe_the_first(self):
        """A duplicate-basename page with no `# Tasks` heading is an empty set, not an eraser."""
        self.first.goal(SELF_GOAL, f"manager_session_id: {ME}\n", ["Own Task"])
        self.first.task("Own Task", f"claude_session_id: {OWN_TASK_SESSION}\n")
        self.second.goal(SELF_GOAL, "", [])

        scope = ss.load(ME, [self.first.root, self.second.root])

        self.assertEqual(scope.goals[SELF_GOAL], {"Own Task"})
        self.assertEqual(scope.exclusion(OWN_TASK_SESSION, ME), (False, ""))


class VaultDirs(unittest.TestCase):
    """The `vault-cli config list` failure branches and the `_probe_root` fallback.

    These are the fail-open paths the module's docstring promises: a broken instrument must
    WIDEN the search, never narrow it, or it reads as "nothing is mine" and silences a
    manager's own gates. Every `ss.load(...)` elsewhere in this file passes an explicit vault
    list, so `_vault_dirs` only ever took its explicit branch and none of this was covered.
    """

    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        patcher = mock.patch.object(ss, "VAULTS_ROOT", self.root.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def stub(self, returncode=0, stdout=""):
        """Replace `vault-cli config list` with a canned result."""
        return mock.patch.object(
            ss.subprocess,
            "run",
            return_value=mock.Mock(returncode=returncode, stdout=stdout),
        )

    def test_a_non_zero_exit_reports_its_own_reason(self):
        with self.stub(returncode=3):
            dirs, reason = ss._vault_dirs()
        self.assertEqual(dirs, [])
        self.assertIn("exited 3", reason)

    def test_unparseable_json_reports_its_own_reason(self):
        with self.stub(stdout="{not json"):
            dirs, reason = ss._vault_dirs()
        self.assertEqual(dirs, [])
        self.assertIn("unparseable JSON", reason)

    def test_a_non_list_payload_reports_its_own_reason(self):
        with self.stub(stdout='{"vaults": []}'):
            dirs, reason = ss._vault_dirs()
        self.assertEqual(dirs, [])
        self.assertIn("something other than a list", reason)

    def test_the_probe_root_widens_to_a_stub_vault_tree(self):
        """A broken config must find MORE vaults, never none — "nothing is mine" is the bug."""
        os.makedirs(os.path.join(self.root.name, "stub-vault", "25 Tasks"))
        os.makedirs(os.path.join(self.root.name, "stub-vault", "24 Goals"))
        with self.stub(returncode=1):
            dirs, reason = ss._vault_dirs()
        self.assertEqual(reason, "")
        self.assertEqual(len(dirs), 1)
        self.assertTrue(dirs[0][0][0].endswith("25 Tasks"), dirs[0][0])


class Cli(unittest.TestCase):
    def test_attributes_named_sessions_and_names_an_unresolved_scope(self):
        import contextlib
        import io

        vault = Vault()
        try:
            vault.goal(SELF_GOAL, f"manager_session_id: {ME}\n", ["Own Task"])
            vault.task("Own Task", f"claude_session_id: {OWN_TASK_SESSION}\n")
            vault.task("Foreign Task", f"claude_session_id: {FOREIGN_SESSION}\n"
                                       "goals:\n    - '[[Some Other Goal]]'\n")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = ss.main(["--self", ME, "--vault", vault.root,
                              "--session", OWN_TASK_SESSION,
                              "--session", FOREIGN_SESSION])
            self.assertEqual(rc, 0)
            self.assertIn("own", out.getvalue())
            self.assertIn("excluded foreign-goal:Some Other Goal", out.getvalue())
            self.assertEqual(err.getvalue(), "")

            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = ss.main(["--self", "00000000-0000-4000-8000-000000000000",
                              "--vault", vault.root])
            self.assertEqual(rc, 0)
            self.assertIn("SCOPE  unresolved:", err.getvalue())
        finally:
            vault.close()

    def test_json_dump_carries_goals_and_sessions(self):
        vault = Vault()
        try:
            vault.goal(SELF_GOAL, f"manager_session_id: {ME}\n", ["Own Task"])
            vault.task("Own Task", f"claude_session_id: {OWN_TASK_SESSION}\n")
            scope = ss.load(ME, [vault.root])
            self.assertEqual(scope.own_titles, {"Own Task", SELF_GOAL})
            self.assertIn(OWN_TASK_SESSION, scope.sessions)
        finally:
            vault.close()
