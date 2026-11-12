#!/usr/bin/env python3
"""Tests for scripts/round-detectors.py — the round's SC1 and SC2 detectors.

The properties worth pinning are the ones where a plausible implementation looks right
and is wrong:

  * **One call per round, one chain build.** A per-row manager resolution measured 387 s;
    the block must build the chain ONCE and only when a row needs routing. A quiet round
    pays nothing.
  * **The join is the SESSION, not an id parsed out of the closer** (SC1 case b) — the
    mirror card lives under an id the closer never mentions.
  * **`🔴 BLOCKED` is read from the WHOLE message**, because `⏰ Ends:` sits on its own
    line — a body-only read routes a row that already names an observable.
  * **A store outage or a failed chain read narrows the block, never `(none)`.** NO-CARD
    is an accusation; a read that did not happen cannot support it.
  * **An unreadable session map exits 2 and says UNKNOWN**, so the sweep cannot render a
    clean `(none)` for a pass that did not run.

Run: python3 -m unittest discover -s scripts/tests
"""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "round-detectors.py")

_spec = importlib.util.spec_from_file_location("round_detectors", _SCRIPT)
rd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rd)

SID_A = "aaaaaaaa-0000-0000-0000-000000000001"
SID_B = "bbbbbbbb-0000-0000-0000-000000000002"
SID_C = "cccccccc-0000-0000-0000-000000000003"
TASK = "Some Blocked Subject"


def _transcript(dirpath, sid, text):
    """Write a one-message transcript and return its path."""
    path = os.path.join(dirpath, f"{sid}.jsonl")
    record = {"type": "assistant", "timestamp": "2026-11-12T12:00:00Z",
              "message": {"content": [{"type": "text", "text": text}]}}
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return path


def _items(rows):
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(rows, f)
    f.close()
    return f.name


def _open_card(sid, item_id="7e55adaf" * 4):
    return {"state": "open", "item_id": item_id, "producer_id": sid}


class TaskTitle(unittest.TestCase):
    def test_the_title_is_the_filename(self):
        self.assertEqual(TASK, rd.task_title(f"/v/25 Tasks/{TASK}.md"))

    def test_no_task_file_is_no_title(self):
        self.assertEqual("", rd.task_title(None))


class SessionClosers(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_no_transcript_is_unreadable(self):
        rows = rd.session_closers({SID_A: {"name": "A"}}, transcript_path=lambda _s: None)
        self.assertEqual(1, len(rows))
        self.assertTrue(rows[0]["unreadable"])

    def test_a_session_with_no_closer_is_skipped(self):
        path = _transcript(self.dir, SID_A, "no panel here")
        rows = rd.session_closers({SID_A: {"name": "A"}}, transcript_path=lambda _s: path)
        self.assertEqual([], rows)

    def test_the_whole_message_is_carried_for_the_blocked_test(self):
        text = "🔴 BLOCKED · the build is red\n👤 You: waiting on the build\n⏰ Ends: CI green"
        path = _transcript(self.dir, SID_A, text)
        rows = rd.session_closers({SID_A: {"name": "A"}}, transcript_path=lambda _s: path)
        self.assertIn("⏰ Ends:", rows[0]["message"])
        self.assertEqual("waiting on the build", rows[0]["body"])
        self.assertIsNone(rows[0]["unreadable"])


class RoutingTitles(unittest.TestCase):
    """The cheap pre-pass: which titles need the chain at all."""

    def _row(self, message, body=None):
        return {"session": SID_A, "name": "A", "task": TASK, "closer": "",
                "body": body if body is not None else message, "message": message,
                "unreadable": None}

    def test_an_ownerless_blocked_row_is_wanted(self):
        self.assertEqual([TASK], rd.routing_titles([self._row("🔴 BLOCKED · x\nwaiting on the build")]))

    def test_a_card_id_is_not_wanted(self):
        self.assertEqual([], rd.routing_titles([self._row("🔴 BLOCKED · x\nwaiting on " + "7e55adaf" * 4)]))

    def test_an_ends_observable_on_its_own_line_is_not_wanted(self):
        row = self._row("🔴 BLOCKED · x\nwaiting\n⏰ Ends: v0.296.0 tagged", body="waiting")
        self.assertEqual([], rd.routing_titles([row]))

    def test_waiting_on_someone_is_wanted(self):
        self.assertEqual([TASK], rd.routing_titles([self._row("🔴 BLOCKED · x\nwaiting on someone")]))

    def test_a_pick_closer_is_not_a_blocked_row(self):
        self.assertEqual([], rd.routing_titles([self._row("👤 You: pick — 1. X · 2. Y")]))

    def test_an_unreadable_row_is_not_wanted(self):
        row = self._row("🔴 BLOCKED · x\nwaiting")
        row["unreadable"] = "no transcript on disk"
        self.assertEqual([], rd.routing_titles([row]))


class Detect(unittest.TestCase):
    """End to end through the rendered block — the shape the digest carries."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.paths = {}

    def _run(self, texts, items, managers=None, resolve_error=None):
        for sid, text in texts.items():
            self.paths[sid] = _transcript(self.dir, sid, text)
        doc = {sid: {"name": f"name-{sid[:4]}", "task_file": f"/v/25 Tasks/{TASK}.md"}
               for sid in texts}
        calls = []

        def resolve(titles):
            calls.append(list(titles))
            if resolve_error:
                raise RuntimeError(resolve_error)
            return managers or {}

        block = rd.detect(doc, fixture=items, transcript_path=lambda s: self.paths.get(s),
                          resolve_many=resolve)
        return block, calls

    # --- SC1 -------------------------------------------------------------------
    def test_a_card_less_waiting_closer_is_no_card(self):
        block, _ = self._run({SID_A: "👤 You: pick — 1. wait · 2. re-run"}, _items([]))
        self.assertIn("no-card=1", block)
        self.assertIn(SID_A[:8], block.split("NO-CARD", 1)[1])

    def test_a_mirror_card_for_the_session_is_not_no_card(self):
        # Case (b): the closer names no id; the store holds a card whose producer is the
        # session. An id-parsing checker would flag this as NO-CARD.
        block, _ = self._run({SID_A: "👤 You: pick — 1. wait · 2. re-run"},
                             _items([_open_card(SID_A)]))
        self.assertIn("no-card=0", block)
        self.assertIn("carded=1", block)

    def test_a_closer_naming_an_absent_id_is_no_card(self):
        block, _ = self._run({SID_A: "👤 You: approve: reply `go` — card " + "deadbeef" * 4},
                             _items([]))
        self.assertIn("no-card=1", block)

    def test_a_routine_closer_is_not_an_ask(self):
        block, _ = self._run({SID_A: "👤 You: nothing — I continue"}, _items([]))
        self.assertIn("no-card=0", block)
        self.assertIn("unknown=0", block)

    def test_an_unreadable_store_narrows_to_unknown_never_no_card(self):
        # `fixture` pointing at a missing file makes the store read fail.
        for sid, text in {SID_A: "👤 You: pick — 1. wait"}.items():
            self.paths[sid] = _transcript(self.dir, sid, text)
        doc = {SID_A: {"name": "A", "task_file": ""}}
        block = rd.detect(doc, fixture="/nonexistent/items.json",
                          transcript_path=lambda s: self.paths.get(s),
                          resolve_many=lambda _t: {})
        self.assertIn("no-card=0", block)
        self.assertIn("unknown=1", block)
        self.assertIn("store unreadable", block)

    # --- SC2 -------------------------------------------------------------------
    def test_an_ownerless_blocked_row_names_the_manager(self):
        block, calls = self._run(
            {SID_A: "🔴 BLOCKED · x\n👤 You: waiting on the build"},
            _items([]), managers={TASK: ("Topic Manager", "chain")})
        self.assertIn("routing=1", block)
        self.assertIn("Topic Manager (chain)", block)
        self.assertEqual([[TASK]], calls)

    def test_a_card_id_manager_or_ends_owner_produces_no_routing(self):
        texts = {
            SID_A: "🔴 BLOCKED · x\n👤 You: waiting on " + "7e55adaf" * 4,
            SID_B: "🔴 BLOCKED · x\n👤 You: escalate to the manager",
            SID_C: "🔴 BLOCKED · x\n👤 You: waiting\n⏰ Ends: CI green",
        }
        block, calls = self._run(texts, _items([]), managers={TASK: ("Topic Manager", "chain")})
        self.assertIn("routing=0", block)
        self.assertEqual([], calls, "the chain must not be built when no row needs routing")

    def test_waiting_on_someone_routes(self):
        block, _ = self._run({SID_A: "🔴 BLOCKED · x\n👤 You: waiting on someone"},
                             _items([]), managers={TASK: ("Topic Manager", "chain")})
        self.assertIn("routing=1", block)

    def test_the_chain_is_built_once_for_every_title(self):
        block, calls = self._run(
            {SID_A: "🔴 BLOCKED · x\n👤 You: waiting on the build",
             SID_B: "🔴 BLOCKED · y\n👤 You: waiting on the deploy"},
            _items([]), managers={TASK: ("Topic Manager", "chain")})
        self.assertEqual(1, len(calls), "one resolve_many call, not one per row")
        self.assertEqual([TASK], calls[0], "a repeated title is requested once")
        self.assertIn("routing=2", block)

    def test_a_failed_chain_read_is_unknown_never_none(self):
        block, _ = self._run({SID_A: "🔴 BLOCKED · x\n👤 You: waiting on the build"},
                             _items([]), resolve_error="session registry unreadable")
        self.assertIn("ROUTING", block)
        self.assertIn("UNKNOWN (manager chain unreadable", block)
        self.assertNotIn("ROUTING (blocked closer naming no owner → the subject's manager)\n  (none)",
                         block)

    def test_a_blank_manager_is_unknown_never_a_nameless_route(self):
        # The exact failure the detector exists to end, reached through its own path: a
        # resolver that yields "" must not print `ROUTE` with no recipient.
        block, _ = self._run({SID_A: "🔴 BLOCKED · x\n👤 You: waiting on the build"},
                             _items([]), managers={TASK: ("", "chain")})
        self.assertIn("routing=0", block)
        self.assertIn("unknown=1", block)
        self.assertIn("routing record names no manager", block)
        self.assertNotIn(" ·  (chain)", block)

    def test_tier_four_renders_fleet(self):
        block, _ = self._run({SID_A: "🔴 BLOCKED · x\n👤 You: waiting on the build"},
                             _items([]), managers={TASK: ("fleet", "fleet")})
        self.assertIn("fleet (fleet)", block)


class Main(unittest.TestCase):
    def _run(self, argv, stdin_text=None):
        buf = io.StringIO()
        stdin = io.StringIO(stdin_text or "")
        orig = rd.sys.stdin
        rd.sys.stdin = stdin
        try:
            with contextlib.redirect_stdout(buf):
                rc = rd.main(argv)
        finally:
            rd.sys.stdin = orig
        return rc, buf.getvalue()

    def test_a_bad_document_exits_two_and_says_unknown(self):
        rc, out = self._run(["--stdin"], stdin_text="{not json")
        self.assertEqual(2, rc)
        self.assertIn("DETECTORS UNKNOWN (pass not run", out)

    def test_a_non_object_document_exits_two(self):
        rc, out = self._run(["--stdin"], stdin_text="[]")
        self.assertEqual(2, rc)
        self.assertIn("UNKNOWN", out)

    def test_a_snapshot_wrapper_with_a_non_object_sessions_exits_two(self):
        # Reading the wrapper as a session map would turn `swept_at` into an accused session.
        rc, out = self._run(["--stdin"], stdin_text='{"swept_at": "x", "sessions": null}')
        self.assertEqual(2, rc)
        self.assertIn("UNKNOWN", out)

    def test_a_snapshot_wrapper_unwraps_its_sessions(self):
        rc, out = self._run(["--stdin"], stdin_text='{"swept_at": "x", "sessions": {}}')
        self.assertEqual(0, rc)
        self.assertIn("DETECTORS  0 closers", out)

    def test_an_empty_document_renders_a_complete_block(self):
        rc, out = self._run(["--stdin"], stdin_text="{}")
        self.assertEqual(0, rc)
        self.assertIn("DETECTORS  0 closers", out)
        self.assertIn("NO-CARD", out)
        self.assertIn("ROUTING", out)


if __name__ == "__main__":
    unittest.main()
