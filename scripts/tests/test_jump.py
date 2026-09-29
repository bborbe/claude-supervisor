#!/usr/bin/env python3
"""Tests for scripts/jump.py's attention queue.

`/supervisor:jump` is the surface a manager actually uses to *reach* a gate, and it
shares who-needs-me.py's parser. It previously re-derived the predicate inline
(`r["kind"] in ("permission", "question")`) instead of calling `is_open_gate()`, so
every rule the feed applies was skipped here — an answered record stayed listed, and
a parked wait, a peer-gate restatement and a finished-work close gate all counted as
gates to jump to. Measured 2026-09-19: the feed dropped panes 223/277 while this
surface still offered them, i.e. the two surfaces disagreed about who needs you.

These pin the queue to the shared classifier, so the next rule added to the feed
cannot silently fail to reach this surface.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import importlib.util
import io
import os
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SCRIPTS, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wnm = _load("who_needs_me", "who-needs-me.py")
jmp = _load("jump", "jump.py")


def rec(pane, detail, kind="question", state="open"):
    return {"session_id": f"sess-{pane}", "pane": str(pane), "cwd": "/tmp",
            "kind": kind, "detail": detail, "ts": 1000 + int(pane), "state": state}


PICK = "pick — 1. do the thing (recommended) · 2. skip"
PARKED = "later (on flying it): refresh head/module stock at the refinery deck"
CLOSE = "approve: /vault-cli:session-close"


class AttentionQueueMatchesTheFeed(unittest.TestCase):
    def setUp(self):
        self._load, self._panes = wnm.load, wnm.wezterm_panes
        self._live_ids, self._age = wnm.live_session_ids, wnm.session_transcript_age
        self._sessions = jmp.pane_sessions
        jmp.pane_sessions = lambda _pmap: {}
        self._me = os.environ.pop("WEZTERM_PANE", None)
        wnm._AGE_CACHE.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        wnm.load, wnm.wezterm_panes = self._load, self._panes
        wnm.live_session_ids, wnm.session_transcript_age = self._live_ids, self._age
        jmp.pane_sessions = self._sessions
        wnm._AGE_CACHE.clear()
        if self._me is not None:
            os.environ["WEZTERM_PANE"] = self._me

    def queue(self, records, status=None, dead=()):
        """`dead` names panes whose session has exited.

        Defaults to none, so every pre-existing case keeps its old meaning; the
        orphan case opts in. Both machine-reading halves of the liveness rule are
        stubbed: the real `live_session_ids` reads `~/.claude/sessions/` and the real
        `session_transcript_age` reads `~/.claude/projects/`, neither of which a unit
        test may depend on. A `dead` session is absent from the registry *and* stale,
        which is the rule's `quiet` verdict -- either signal alone would be wrong.
        """
        dead_panes = {str(p) for p in dead}
        pmap = {str(r["pane"]): {"pane_id": int(r["pane"]), "title": "t"} for r in records}
        wnm.load = lambda _suffix: records
        wnm.wezterm_panes = lambda: pmap
        wnm.live_session_ids = lambda _d=None: {
            r["session_id"] for r in records if str(r["pane"]) not in dead_panes
        }
        wnm.session_transcript_age = lambda sid: (
            float("inf") if any(r["session_id"] == sid and str(r["pane"]) in dead_panes
                                for r in records) else 0)
        wnm.task_status_from_closer = status or (lambda _rec: None)
        return [r["pane"] for r in jmp.attention_queue(wnm, pmap)]

    def test_parked_wait_is_not_offered(self):
        """Class 2 — the feed drops it, so the jump surface must too."""
        self.assertEqual(self.queue([rec(223, PARKED)]), [])

    def test_open_pick_is_offered(self):
        self.assertEqual(self.queue([rec(208, PICK)]), ["208"])

    def test_answered_record_is_not_offered(self):
        """The check #30 added to the feed and never propagated here."""
        self.assertEqual(self.queue([rec(244, PICK, state="answered")]), [])

    def test_close_gate_on_finished_task_is_not_offered(self):
        """Class 4 — reapable, not something to jump to."""
        self.assertEqual(self.queue([rec(230, CLOSE)], status=lambda _r: "completed"), [])

    def test_close_gate_on_open_task_is_offered(self):
        self.assertEqual(self.queue([rec(230, CLOSE)], status=lambda _r: "in_progress"), ["230"])

    def test_peer_restatement_is_not_offered(self):
        """Class 3 — pane 285 already carries the gate."""
        records = [rec(285, PICK), rec(900, "the approval in pane 285 — yours alone")]
        self.assertEqual(self.queue(records), ["285"])

    def test_orphaned_item_is_not_offered(self):
        """Class 5 — the session exited (absent from the registry AND stale).

        This surface re-derived the feed's filter as pane-existence, so an item whose
        session was gone stayed jumpable here long after the feed had dropped it --
        activating the pane would land the operator on whatever now wears that id.
        """
        records = [rec(208, PICK), rec(300, PICK)]
        self.assertEqual(self.queue(records, dead=[300]), ["208"])

    def test_live_headless_worker_is_still_offered(self):
        """The regression guard — absent from the registry is NOT dead.

        A headless worker holds no registry entry by construction, so a filter keyed
        on registry absence alone would hide every gate it raises from this surface
        too. Its fresh transcript is what keeps it.
        """
        records = [rec(846, PICK)]
        pmap = {"846": {"pane_id": 846, "title": "t"}}
        wnm.load = lambda _s: records
        wnm.wezterm_panes = lambda: pmap
        wnm.live_session_ids = lambda _d=None: set()          # absent, as headless is
        wnm.session_transcript_age = lambda _sid: 30          # ...but writing right now
        wnm.task_status_from_closer = lambda _rec: None
        self.assertEqual([r["pane"] for r in jmp.attention_queue(wnm, pmap)], ["846"])

    def test_queue_matches_the_feed_on_a_mixed_set(self):
        """The property that actually matters: the two surfaces cannot disagree."""
        records = [
            rec(208, PICK), rec(223, PARKED), rec(244, PICK, state="answered"),
            rec(230, CLOSE), rec(253, "Bash: git status", kind="permission"),
        ]
        status = lambda r: "completed" if r["pane"] == "230" else None
        pmap = {str(r["pane"]): {} for r in records}
        wnm.load = lambda _suffix: records
        wnm.wezterm_panes = lambda: pmap
        wnm.task_status_from_closer = status

        needs = [wnm.reclassify_idle(r) for r in records]
        open_panes = {str(r.get("pane")) for r in needs if wnm.is_open_gate(r, task_status=status)}
        feed = sorted(str(r["pane"]) for r in needs
                      if wnm.is_open_gate(r, open_panes=open_panes, task_status=status))
        self.assertEqual(sorted(self.queue(records, status=status)), feed)


class _WnmStub:
    """The slice of who-needs-me.py's surface the `--list` path reaches."""

    def name_of(self, _rec, _pmap):
        return "Session"

    def age(self, _ts):
        return "1m"


class JumpTransportTest(unittest.TestCase):
    """jump.py's three pane-read states, end to end through `main()`.

    `wezterm_panes()` returned `{}` for both a failed `wezterm cli list` and a
    reachable WezTerm holding no panes, so `main()`'s `if not pmap:` gate could not
    tell them apart and refused both with the same message and the same exit code.
    Measured 2026-09-26 against origin/master: with the mux socket unreachable and
    with a reachable-but-empty transport, stderr was byte-identical (71B) and both
    exited 1 -- a healthy empty fleet reported as a broken transport.

    All three states are asserted together because any two of them can be satisfied
    by a wrong implementation: refusing on `not pmap` passes the broken case and
    fails the empty one; never refusing passes both healthy cases and fails the
    broken one.
    """

    # `wezterm_panes()` returns `pane_id -> pane`, not the raw array `wezterm` prints.
    PANES = {"5": {"pane_id": 5, "tab_id": 1, "window_id": 0, "title": "Session",
                   "tty_name": "/dev/ttys001"}}

    def run_jump(self, panes):
        """Run `main()` with `--list` against a stubbed transport; (rc, out, err)."""
        records = [rec(5, PICK)]
        patches = [
            mock.patch.object(jmp, "wezterm_panes", lambda: panes),
            mock.patch.object(jmp, "load_wnm", _WnmStub),
            # No live panes means no live session means no attention row -- which is
            # why the empty case must print an empty queue, not a refusal.
            mock.patch.object(jmp, "attention_queue",
                              lambda _w, pmap: list(records) if pmap else []),
            mock.patch.object(jmp, "load_visited", dict),
            mock.patch.object(jmp.sys, "argv", ["jump.py", "--list"]),
        ]
        out, err = io.StringIO(), io.StringIO()
        for p in patches:
            p.start()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = jmp.main()
        finally:
            for p in patches:
                p.stop()
        return rc, out.getvalue(), err.getvalue()

    def test_unreadable_transport_is_attributed_to_the_read(self):
        """`None` from the transport refuses, and says the READ failed."""
        rc, out, err = self.run_jump(None)
        self.assertEqual(1, rc)
        self.assertIn("wezterm cli list` unreadable", err)
        self.assertEqual("", out)

    def test_healthy_transport_with_panes_is_unchanged(self):
        """The healthy path is untouched: the queue prints and the exit stays 0."""
        rc, out, err = self.run_jump(self.PANES)
        self.assertEqual(0, rc, err)
        self.assertIn("Needs you (1, newest first)", out)

    def test_empty_but_reachable_transport_is_not_a_failure(self):
        """A reachable WezTerm with no panes is an answer, not a broken query.

        The control for the `is None` test: refusing on `not pmap` would pass the
        broken-transport case above and wrongly fail this one.
        """
        rc, out, err = self.run_jump({})
        self.assertEqual(0, rc, err)
        self.assertEqual("", err)
        self.assertIn("Nothing needs you.", out)

    def test_broken_and_empty_are_distinguishable(self):
        """The defect itself: the two states must not answer identically."""
        broken, empty = self.run_jump(None), self.run_jump({})
        self.assertNotEqual(broken[0], empty[0])
        self.assertNotEqual(broken[2], empty[2])


if __name__ == "__main__":
    unittest.main()
