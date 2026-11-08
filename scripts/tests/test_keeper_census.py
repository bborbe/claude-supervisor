#!/usr/bin/env python3
"""Tests for scripts/keeper-census.py — the three fixtures, the red paths and the card.

The census joins two populations that no live environment can be bent into on demand (the
session registry and a vault's page stamps), so both probes are injected: `read_live` returns
the registry the test declares, `cadence` the loop state per subject. Everything else — the
page scan, the tier rules, the red verdict, the exit code and the card dedup — is the real
code, which is the point: a fixture that stubbed the census would assert nothing about it.

⚠️ **The verdicts are injected, never manufactured.** A fabricated session id answers
`UNKNOWN` from the real instrument, so a test that invented one and asserted `kept` would be
asserting a state the instrument never produces (`test_worker_keepalive.py` gives the same
reason).
"""

import importlib.util
import io
import pathlib
import tempfile
import unittest
from contextlib import redirect_stdout

_HERE = pathlib.Path(__file__).resolve().parent

#: A keeper-leg file carrying the topic and worker legs and NOT the goal leg — the shape
#: `agents/fleet-keepalive-actor.md` ships today (`docs/session-tiers.md` § Keep-alive,
#: "Goal manager — goal in no topic": ⚠️ none yet).
LEGS_NO_GOAL = """\
[ "--vault", vault, "--subject", name, "--branch", "topic", "--dir", topics ]
[ "--vault", vault, "--task", name, "--dir", tasks, "--goals-dir", goals,
  "worker-keepalive.py" ]
"""

LEGS_ALL = LEGS_NO_GOAL + """\
[ "--vault", vault, "--subject", name, "--branch", "goal" ]
"""


def load():
    spec = importlib.util.spec_from_file_location(
        "keeper_census", str(_HERE.parent / "keeper-census.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def page(kind, name, sid="", status="in_progress", phase="execution", goals=(), topics=(),
         body=""):
    lines = ["---", f"page_type: {kind}", f"status: {status}", f"phase: {phase}"]
    if sid:
        lines.append(f"claude_session_id: {sid}")
    if goals:
        lines.append("goals:")
        lines += [f"    - '[[{g}]]'" for g in goals]
    if topics:
        lines.append("topics:")
        lines += [f"    - '[[{t}]]'" for t in topics]
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body


class CensusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.vault = self.root / "vault"
        for name in ("23 Topics", "24 Goals", "25 Tasks"):
            (self.vault / name).mkdir(parents=True)
        self.state = self.root / "state"
        self.state.mkdir()

        self.k = load()
        self.k.STATE_DIR = str(self.state)
        self.k.EPISODES = str(self.state / "episodes.json")
        self.live = {}
        self.states = {}
        self.posts = []
        self.k.read_live = lambda: dict(self.live)
        self.k.fleet_session = lambda: None
        self.k.cadence = lambda subject, branch, vault: self.states.get(
            (branch, subject), "UNREADABLE"
        )
        self.real_notify = self.k.notify
        self.k.notify = lambda key, payload, options, context="": (
            self.posts.append((key, payload)) or f"item-{len(self.posts)}"
        )
        self.legs = self.root / "legs.md"
        self.legs.write_text(LEGS_NO_GOAL, encoding="utf-8")

    # -- fixture helpers ---------------------------------------------------------------

    def write(self, kind, name, **kwargs):
        directory = {"topic": "23 Topics", "goal": "24 Goals", "task": "25 Tasks"}[kind]
        (self.vault / directory / f"{name}.md").write_text(
            page(kind, name, **kwargs), encoding="utf-8"
        )

    def alive(self, sid, name="session"):
        self.live[sid] = {"sessionId": sid, "name": name}

    def run_census(self, *extra):
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.k.main(["--vault", str(self.vault),
                                "--leg-file", str(self.legs), *extra])
        return code, out.getvalue()

    def rows(self, text):
        return [ln for ln in text.splitlines()
                if ln.startswith("      live") or ln.startswith("      not-live")]

    # -- SC1: both populations get a row, and the count tracks the fixture ---------------

    def test_two_rows_then_one_row_plus_mismatch(self):
        self.write("topic", "T", sid="topic-sid")
        self.write("goal", "G", sid="goal-sid")
        self.alive("topic-sid", "manager")
        self.states[("topic", "T")] = "OK"

        code, text = self.run_census("--no-card")
        rows = self.rows(text)
        self.assertEqual(2, len(rows), text)
        self.assertIn("topic-mgr", rows[0])
        self.assertIn("goal-mgr", rows[1])
        self.assertIn("keeper-census: unkept=0 double-kept=0 sessions=1", text)
        self.assertIn('MISMATCH page "G" records session goal-sid', text)
        self.assertEqual(0, code)

        # The same fixture with the live session removed — its registry entry and its page
        # both gone — prints ONE row, and the mismatch names the session the registry does
        # not hold. The row count is what the census is asserting, so it is read both ways.
        self.live.clear()
        (self.vault / "23 Topics" / "T.md").unlink()
        code, text = self.run_census("--no-card")
        self.assertEqual(1, len(self.rows(text)), text)
        self.assertIn("keeper-census: unkept=0 double-kept=0 sessions=0", text)
        self.assertIn('MISMATCH page "G" records session goal-sid', text)
        self.assertEqual(0, code)

    # -- SC3: red rows are named, and the exit code is non-zero -------------------------

    def test_unkept_exits_non_zero_and_names_the_row(self):
        self.write("goal", "Topicless", sid="goal-sid")
        self.alive("goal-sid", "manager")
        self.states[("goal", "Topicless")] = "OK"

        code, text = self.run_census("--no-card")
        self.assertEqual(1, code)
        self.assertIn("RED unkept", text)
        self.assertIn("Topicless", text)
        self.assertIn("goal is in no topic and the fleet round has no goal keep-alive leg", text)
        self.assertIn("keeper-census: unkept=1 double-kept=0 sessions=1", text)

    def test_double_kept_names_both_claimants(self):
        self.write("topic", "T1", sid="t1-sid", body="## Goals\n- [[Shared]]\n\n## Non-goals\n")
        self.write("topic", "T2", sid="t2-sid", body="## Goals\n- [[Shared]]\n\n## Non-goals\n")
        self.write("goal", "Shared", sid="g-sid")
        for sid, name in (("t1-sid", "m1"), ("t2-sid", "m2"), ("g-sid", "m3")):
            self.alive(sid, name)
        self.states[("topic", "T1")] = "OK"
        self.states[("topic", "T2")] = "OK"

        code, text = self.run_census("--no-card")
        self.assertEqual(1, code)
        self.assertIn("RED double-kept", text)
        self.assertIn("topic mgr: T1", text)
        self.assertIn("topic mgr: T2", text)
        self.assertIn("keeper-census: unkept=0 double-kept=1 sessions=3", text)

    def test_fully_kept_exits_zero(self):
        self.write("topic", "T", sid="topic-sid", body="## Goals\n- [[G]]\n\n## Non-goals\n")
        self.write("goal", "G", sid="goal-sid")
        self.write("task", "W", sid="worker-sid", goals=["G"])
        for sid in ("topic-sid", "goal-sid", "worker-sid"):
            self.alive(sid)
        self.states[("topic", "T")] = "OK"
        self.states[("goal", "G")] = "OK"

        code, text = self.run_census("--no-card")
        self.assertEqual(0, code)
        self.assertEqual(3, len(self.rows(text)), text)
        self.assertNotIn("RED", text)
        self.assertEqual("keeper-census: unkept=0 double-kept=0 sessions=3",
                         text.strip().splitlines()[-1])

    # -- SC5: the three named fixtures ---------------------------------------------------

    def test_topicless_goal_manager_with_no_fleet_leg_reads_unkept(self):
        """Fixture 1 — a topic-less goal manager whose fleet leg is missing."""
        self.write("goal", "Orphan", sid="g-sid")
        self.alive("g-sid")
        self.states[("goal", "Orphan")] = "OK"

        code, text = self.run_census("--no-card")
        self.assertEqual(1, code)
        self.assertRegex(text, r"RED unkept\s+goal-mgr\s+Orphan")

        # The same row goes green the moment the leg exists — the census reads the file,
        # it does not hardcode today's gap.
        self.legs.write_text(LEGS_ALL, encoding="utf-8")
        code, text = self.run_census("--no-card")
        self.assertEqual(0, code)
        self.assertIn("keeper: fleet round", text)

    def test_a_topicless_goal_outside_the_fleet_legs_population_reads_unkept(self):
        """`crashed-managers.fleet_keeps` selects `in_progress` / `execution` only."""
        self.legs.write_text(LEGS_ALL, encoding="utf-8")
        self.write("goal", "Waiting", sid="g-sid", phase="todo")
        self.alive("g-sid")
        self.states[("goal", "Waiting")] = "OK"

        code, text = self.run_census("--no-card")
        self.assertEqual(1, code)
        self.assertIn("outside the fleet leg's population", text)

    def test_live_stopped_goal_manager_leaves_its_worker_unkept(self):
        """Fixture 2 — goal manager LIVE, loop STOPPED, worker with no other keeper."""
        self.write("topic", "T", sid="topic-sid", body="## Goals\n- [[G]]\n\n## Non-goals\n")
        self.write("goal", "G", sid="goal-sid")
        self.write("task", "W", sid="worker-sid", goals=["G"])
        for sid in ("topic-sid", "goal-sid", "worker-sid"):
            self.alive(sid)
        self.states[("topic", "T")] = "OK"
        self.states[("goal", "G")] = "STOPPED"

        code, text = self.run_census("--no-card")
        self.assertEqual(1, code)
        self.assertRegex(text, r"RED unkept\s+worker\s+W")
        self.assertIn("goal manager session LIVE but its loop is not running: G [STOPPED]", text)
        # The goal manager itself is still kept by its topic manager — only the worker is red.
        self.assertIn("keeper-census: unkept=1 double-kept=0 sessions=3", text)

    def test_fully_kept_fleet_reads_zero(self):
        """Fixture 3 — the same shape with the goal loop running: nothing red."""
        self.write("topic", "T", sid="topic-sid", body="## Goals\n- [[G]]\n\n## Non-goals\n")
        self.write("goal", "G", sid="goal-sid")
        self.write("task", "W", sid="worker-sid", goals=["G"])
        for sid in ("topic-sid", "goal-sid", "worker-sid"):
            self.alive(sid)
        self.states[("topic", "T")] = "OK"
        self.states[("goal", "G")] = "STALE"   # STALE owns, per § Owner

        code, text = self.run_census("--no-card")
        self.assertEqual(0, code)
        self.assertIn("keeper-census: unkept=0 double-kept=0 sessions=3", text)

    # -- SC4: one card per red reading, none on the second consecutive run ---------------

    def test_first_red_run_posts_one_card_and_the_second_posts_none(self):
        self.write("goal", "Topicless", sid="goal-sid")
        self.alive("goal-sid")
        self.states[("goal", "Topicless")] = "OK"

        code, _ = self.run_census("--now", "1000000")
        self.assertEqual(1, code)
        self.assertEqual(1, len(self.posts), self.posts)
        key, payload = self.posts[0]
        self.assertIn("Topicless", payload)
        self.assertIn("unkept", payload)
        self.assertTrue(key.startswith("keeper-census-red-1000000-d0"), key)

        code, _ = self.run_census("--now", "1000060")
        self.assertEqual(1, code)
        self.assertEqual(1, len(self.posts), "a still-red census must not re-ask")

        # Fixed, then broken again: a NEW episode, so the card is asked again under a new key.
        self.legs.write_text(LEGS_ALL, encoding="utf-8")
        code, _ = self.run_census("--now", "1000120")
        self.assertEqual(0, code)
        self.assertEqual(1, len(self.posts))
        self.assertFalse((self.state / "episodes.json").exists(),
                         "a green census ends the episode and leaves no record")

        self.legs.write_text(LEGS_NO_GOAL, encoding="utf-8")
        self.run_census("--now", "1000180")
        self.assertEqual(2, len(self.posts))
        self.assertNotEqual(self.posts[0][0], self.posts[1][0])

    def test_the_fleet_manager_is_kept_by_the_operator(self):
        """The one row in the chain with no manager above it — it must never read red."""
        self.k.fleet_session = lambda: "fm-sid"
        self.alive("fm-sid", "Fleet Manager")

        code, text = self.run_census("--no-card")
        self.assertEqual(0, code)
        self.assertRegex(text, r"fleet-mgr\s+Fleet Manager\s+keeper: operator")
        self.assertIn("keeper-census: unkept=0 double-kept=0 sessions=1", text)

    # -- the two parsing defects the review caught --------------------------------------

    def test_a_goals_section_that_ends_the_file_still_declares_its_goals(self):
        """`## Goals` as the last section must still declare — the section ends at EOF."""
        self.write("topic", "T", sid="t-sid", body="## Goals\n- [[G]]\n")
        self.write("goal", "G", sid="g-sid")
        self.write("task", "W", sid="w-sid", goals=["G"])
        for sid in ("t-sid", "g-sid", "w-sid"):
            self.alive(sid)
        self.states[("topic", "T")] = "OK"
        self.states[("goal", "G")] = "OK"

        code, text = self.run_census("--no-card")
        self.assertEqual(0, code, text)
        self.assertIn("keeper: topic mgr: T", text)

    def test_an_empty_session_id_does_not_swallow_the_next_key(self):
        """`claude_session_id:` with no value must read as absent, not as the next key."""
        raw = ("---\npage_type: task\nstatus: in_progress\nclaude_session_id:\n"
               "phase: execution\n---\n\nbody\n")
        (self.vault / "25 Tasks" / "Empty.md").write_text(raw, encoding="utf-8")

        code, text = self.run_census("--no-card")
        self.assertEqual(0, code, text)
        self.assertNotIn("Empty", text)
        self.assertIn("sessions=0", text)

    def test_a_null_session_id_is_not_a_session(self):
        raw = ("---\npage_type: task\nstatus: in_progress\nclaude_session_id: null\n---\n\nbody\n")
        (self.vault / "25 Tasks" / "Null.md").write_text(raw, encoding="utf-8")

        code, text = self.run_census("--no-card")
        self.assertEqual(0, code, text)
        self.assertNotIn("Null", text)

    # -- the third answer and the fourth state -------------------------------------------

    def test_an_unreadable_loop_probe_is_cannot_tell_never_unkept(self):
        self.write("topic", "T", sid="t-sid", body="## Goals\n- [[G]]\n")
        self.write("goal", "G", sid="g-sid")
        self.write("task", "W", sid="w-sid", goals=["G"])
        for sid in ("t-sid", "g-sid", "w-sid"):
            self.alive(sid)
        self.states[("topic", "T")] = "OK"
        self.states[("goal", "G")] = "UNREADABLE"

        code, text = self.run_census("--no-card")
        self.assertEqual(0, code, text)
        self.assertNotIn("RED", text)
        self.assertIn("cannot-tell=1", text)
        self.assertIn("G [UNREADABLE]", text)

    def test_a_live_session_no_page_records_is_unrecorded_not_unkept(self):
        """Outside the tree is reported and named, never counted as a keeper gap."""
        self.alive("stray-sid", "private-personal-xx")

        code, text = self.run_census("--no-card")
        self.assertEqual(0, code, text)
        self.assertIn("unrecorded=1", text)
        self.assertIn("keeper-census: unkept=0 double-kept=0 sessions=1", text)
        self.assertIn("MISMATCH live session stray-sid (private-personal-xx) — no page records it",
                      text)

    # -- the card's argv ---------------------------------------------------------------

    def test_notify_pins_the_attention_argv(self):
        """A wrong producer id or kind posts no card while every other test stays green."""
        seen = {}

        class Done:
            stdout = "ITEM_ID: abc123\n"

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return Done()

        original, self.k.subprocess.run = self.k.subprocess.run, fake_run
        self.addCleanup(lambda: setattr(self.k.subprocess, "run", original))

        item = self.real_notify("key-1", "payload", ["acknowledge"], context="ctx")
        self.assertEqual("abc123", item)
        argv = seen["argv"]
        self.assertIn("--producer-id", argv)
        self.assertEqual("keeper-census", argv[argv.index("--producer-id") + 1])
        self.assertEqual("manager", argv[argv.index("--producer-kind") + 1])
        self.assertEqual("key-1", argv[argv.index("--dedup-key") + 1])
        self.assertEqual(["acknowledge"], [argv[i + 1] for i, a in enumerate(argv)
                                           if a == "--option"])

    # -- the card's day bucket ------------------------------------------------------------

    def test_a_still_red_census_reposts_on_the_next_day(self):
        """The card expires after 24 h, so a fleet red a day later must be asked again."""
        self.write("goal", "Topicless", sid="goal-sid")
        self.alive("goal-sid")
        self.states[("goal", "Topicless")] = "OK"

        self.run_census("--now", "1000000")
        self.assertEqual(1, len(self.posts))
        self.run_census("--now", str(1000000 + 86400))
        self.assertEqual(2, len(self.posts), self.posts)
        self.assertTrue(self.posts[1][0].endswith("-d1"), self.posts[1][0])

    def test_the_count_line_stays_last_even_when_a_card_posts(self):
        self.write("goal", "Topicless", sid="goal-sid")
        self.alive("goal-sid")
        self.states[("goal", "Topicless")] = "OK"

        _, text = self.run_census("--now", "1000000")
        self.assertEqual("keeper-census: unkept=1 double-kept=0 sessions=1",
                         text.strip().splitlines()[-1])
        self.assertIn("  card: item-1", text)

    # -- refusals ------------------------------------------------------------------------

    def test_unreadable_registry_is_a_refusal_never_a_clean_fleet(self):
        self.k.read_live = lambda: None
        code, text = self.run_census("--no-card")
        self.assertEqual(2, code)
        self.assertEqual("", text)

    def test_missing_vault_is_a_refusal(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.k.main(["--vault", str(self.root / "nope"), "--no-card"])
        self.assertEqual(2, code)

    # -- leg detection -------------------------------------------------------------------

    def test_legs_are_read_from_the_file_not_assumed(self):
        self.assertEqual({"topic": True, "goal": False, "worker": True},
                         self.k.legs(str(self.legs)))
        self.legs.write_text(LEGS_ALL, encoding="utf-8")
        self.assertEqual({"topic": True, "goal": True, "worker": True},
                         self.k.legs(str(self.legs)))

    def test_terminal_pages_are_not_censused(self):
        self.write("goal", "Done", sid="gone-sid", status="completed")
        code, text = self.run_census("--no-card")
        self.assertEqual(0, code)
        self.assertNotIn("Done", text)
        self.assertIn("sessions=0", text)


if __name__ == "__main__":
    unittest.main()
