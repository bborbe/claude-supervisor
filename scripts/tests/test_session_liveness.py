#!/usr/bin/env python3
"""Tests for scripts/session-liveness.py — the endpoint-only liveness instrument.

Liveness is answered by the attention store's `session-heartbeat` endpoint alone (since
2026-10-08; the registry read moved to `session-identity.py`, whose own suite is
`test_session_identity.py`). So the whole suite runs against a **fixture endpoint** rather than
the live store — a real session on this machine must not be able to turn an ABSENT assertion
into LIVE.

The load-bearing properties, in the order the 2026-09-26 near-miss ranks them:

  * a prefix and the full id it abbreviates return the SAME verdict — a probe that answers
    differently for the two is the defect;
  * an unreachable endpoint, or a response that is not the store's 404, is UNKNOWN, never
    ABSENT — a caller that folds an I/O error into "not live" resumes onto a live conversation;
  * a readable endpoint that positively reports an id as not live IS ABSENT — a 404, or a row
    whose `live` is false;
  * an ambiguous prefix refuses rather than guessing, and names candidates a caller can tell
    apart;
  * `--list --json` carries the identity fields `/supervisor:open` joins on.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from endpoint_fixture import FixtureEndpoint, dead_url, row

_HERE = os.path.dirname(os.path.abspath(__file__))

LIVE, ABSENT, UNKNOWN, AMBIGUOUS = 0, 1, 2, 3


def load():
    spec = importlib.util.spec_from_file_location(
        "session_liveness", os.path.join(os.path.dirname(_HERE), "session-liveness.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SessionLiveness(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.tmp = tempfile.TemporaryDirectory()
        # An identity registry the `--list` join reads names from. It is isolated so the
        # machine's real registry cannot supply a name a fixture did not plant.
        self.registry = os.path.join(self.tmp.name, "registry")
        os.makedirs(self.registry)
        # The start-time cache is keyed by PID, so the same isolation rule applies: a suite
        # reading the real one would answer for whatever process last held that number.
        self._prior_cache_env = os.environ.get("SUPERVISOR_START_CACHE")
        os.environ["SUPERVISOR_START_CACHE"] = os.path.join(self.tmp.name, "starts.json")

    def tearDown(self):
        if self._prior_cache_env is None:
            os.environ.pop("SUPERVISOR_START_CACHE", None)
        else:
            os.environ["SUPERVISOR_START_CACHE"] = self._prior_cache_env
        self.tmp.cleanup()

    def plant(self, session_id, pid=None, name="synth", **extra):
        """A registry record — the identity half of `--list`. Any live pid works for names.

        ⚠️ **Named by session id, not pid.** Two planted sessions in one case would otherwise
        collide on the same `<pid>.json` and the second would overwrite the first; the reader
        globs `*.json` and keys on the record's own `sessionId`, so the filename is free.
        """
        rec = {"sessionId": session_id, "pid": pid or os.getpid(), "name": name, "status": "busy"}
        rec.update(extra)
        with open(os.path.join(self.registry, "%s.json" % session_id), "w", encoding="utf-8") as fh:
            json.dump(rec, fh)

    def check(self, session_id, endpoint):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.m.main(["--check", session_id, "--endpoint", endpoint])
        return rc, out.getvalue() + err.getvalue()

    def listing(self, endpoint, directory=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.m.main(
                ["--list", "--endpoint", endpoint, "--dir", directory or self.registry]
            )
        return rc, out.getvalue() + err.getvalue()

    def json_listing(self, endpoint, directory=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.m.main(
                ["--list", "--json", "--endpoint", endpoint, "--dir", directory or self.registry]
            )
        return rc, json.loads(out.getvalue())

    # ---- the falsifier: prefix and full id agree -------------------------------------

    def test_prefix_and_full_id_return_the_same_verdict(self):
        # The 2026-09-26 near-miss: a prefix read as ABSENT while its session was live. The
        # per-id route 404s on a prefix (it is an exact lookup), so a unique prefix must
        # resolve through the list route to the same verdict the full id gets.
        sid = "3fd529af-1111-2222-3333-444455556666"
        with FixtureEndpoint([row(sid)]) as fx:
            full = self.check(sid, fx.url)
            pref = self.check("3fd529af", fx.url)
        self.assertEqual(full[0], LIVE)
        self.assertEqual(pref[0], LIVE)
        self.assertEqual(full[0], pref[0], "prefix verdict must equal full-id verdict")

    def test_absent_id_differs_from_a_resolving_prefix(self):
        # (b) and (c) must NOT agree, or the probe is a constant-return stub.
        sid = "3fd529af-1111-2222-3333-444455556666"
        with FixtureEndpoint([row(sid)]) as fx:
            self.assertEqual(self.check("3fd529af", fx.url)[0], LIVE)
            self.assertEqual(self.check("deadbeef", fx.url)[0], ABSENT)

    def test_prefix_match_is_case_insensitive(self):
        with FixtureEndpoint([row("ABCDEF12-1111-2222-3333-444455556666")]) as fx:
            self.assertEqual(self.check("abcdef12", fx.url)[0], LIVE)

    # ---- UNKNOWN vs ABSENT: the dangerous direction -----------------------------------

    def test_a_404_is_absent_not_unknown(self):
        # The store positively reporting it holds no such session — a READABLE endpoint that
        # licenses ABSENT.
        with FixtureEndpoint([]) as fx:
            rc, out = self.check("does-not-exist-xyz", fx.url)
        self.assertEqual(rc, ABSENT)
        self.assertIn("ABSENT", out)

    def test_unreachable_endpoint_is_unknown_not_absent(self):
        # The SC4 direction. A dead socket means the probe could not run, and folding that into
        # "not live" is permission to resume onto a live conversation.
        rc, out = self.check("3fd529af", dead_url())
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("UNKNOWN", out)

    def test_a_non_200_non_404_status_is_unknown_not_absent(self):
        # A 500 from the list route (reached when the per-id route answered 404) is not the
        # store saying "gone" — it is the store failing, which is UNKNOWN.
        with FixtureEndpoint([], list_status=500) as fx:
            rc, out = self.check("3fd529af", fx.url)
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("UNKNOWN", out)

    def test_a_stale_row_is_absent_not_live(self):
        # A row the store holds but marks not live — positively reported, so ABSENT is licensed.
        sid = "57a1e111-1111-2222-3333-444455556666"
        with FixtureEndpoint([row(sid, live=False)]) as fx:
            rc, out = self.check(sid, fx.url)
        self.assertEqual(rc, ABSENT)
        self.assertIn("ABSENT", out)

    def test_a_row_without_a_live_flag_is_unknown(self):
        # The store always sends `live`; a row that carries something else is a store this
        # reader does not understand, and "I do not understand this row" is never ABSENT.
        sid = "n0f1a600-1111-2222-3333-444455556666"
        bad = row(sid)
        bad["live"] = None
        with FixtureEndpoint([bad]) as fx:
            rc, out = self.check(sid, fx.url)
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("no usable `live` flag", out)

    # ---- ambiguity ---------------------------------------------------------------------

    def colliding_pair(self):
        """Two sessions sharing an 8-char prefix."""
        return [
            row("abc12345-1111-2222-3333-444455556666"),
            row("abc12345-9999-8888-7777-666655554444"),
        ]

    def test_ambiguous_prefix_refuses_rather_than_guesses(self):
        with FixtureEndpoint(self.colliding_pair()) as fx:
            rc, out = self.check("abc12345", fx.url)
        self.assertEqual(rc, AMBIGUOUS)
        self.assertIn("pass a longer id", out)

    def test_ambiguous_candidates_are_distinguishable(self):
        # Echoing 8 chars back hands the caller two identical strings and no way to choose.
        with FixtureEndpoint(self.colliding_pair()) as fx:
            _, out = self.check("abc12345", fx.url)
        self.assertIn("abc12345-111", out)
        self.assertIn("abc12345-999", out)

    def test_a_longer_prefix_disambiguates(self):
        with FixtureEndpoint(self.colliding_pair()) as fx:
            self.assertEqual(self.check("abc12345-1111", fx.url)[0], LIVE)
            self.assertEqual(self.check("abc12345-9999", fx.url)[0], LIVE)

    # ---- --list ------------------------------------------------------------------------

    def test_list_shows_only_live_sessions(self):
        self.plant("aaaa1111-1111-2222-3333-444455556666", name="Alive")
        self.plant("bbbb2222-1111-2222-3333-444455556666", name="Dead")
        rows = [
            row("aaaa1111-1111-2222-3333-444455556666"),
            row("bbbb2222-1111-2222-3333-444455556666", live=False),
        ]
        with FixtureEndpoint(rows) as fx:
            rc, out = self.listing(fx.url)
        self.assertEqual(rc, LIVE)
        self.assertIn("Alive", out)
        self.assertNotIn("Dead", out)

    def test_list_includes_a_row_with_no_identity_entry(self):
        # A headless or cluster worker holds a heartbeat row and no registry entry; it must
        # still appear, with an empty name rather than being dropped.
        sid = "11571e57-1111-2222-3333-444455556666"
        with FixtureEndpoint([row(sid)]) as fx:
            rc, out = self.listing(fx.url)
        self.assertEqual(rc, LIVE)
        self.assertIn(sid, out)

    def test_list_on_an_unreachable_endpoint_is_unknown(self):
        rc, out = self.listing(dead_url())
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("UNKNOWN", out)

    def test_list_on_an_unreadable_registry_is_unknown(self):
        # The names half is load-bearing: a names-less list makes `/supervisor:open`'s
        # topic->manager join match nothing and spawn a SECOND manager onto a live topic.
        with FixtureEndpoint([row("aaaa1111-1111-2222-3333-444455556666")]) as fx:
            rc, out = self.listing(fx.url, directory=os.path.join(self.tmp.name, "does-not-exist"))
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("UNKNOWN", out)

    # ---- --list --json: the shape `/supervisor:open` reads -------------------------------

    def test_json_listing_carries_former_names_and_cwd(self):
        # `/supervisor:open` resolves a topic's manager by matching the topic against the
        # current name AND every name the session has held, then spawns into `cwd`.
        sid = "f0rmer00-1111-2222-3333-444455556666"
        self.plant(
            sid,
            name="Renamed Topic Manager",
            formerNames=[{"name": "Topic Manager", "at": "2026-09-18"}],
            cwd="/Users/bborbe/Documents/workspaces/thing",
            nameSource="user",
        )
        with FixtureEndpoint([row(sid)]) as fx:
            rc, rows = self.json_listing(fx.url)
        self.assertEqual(rc, LIVE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Renamed Topic Manager")
        self.assertEqual(rows[0]["formerNames"], ["Topic Manager"])
        self.assertEqual(rows[0]["cwd"], "/Users/bborbe/Documents/workspaces/thing")
        self.assertEqual(rows[0]["nameSource"], "user")

    def test_json_listing_marks_the_source(self):
        sid = "bea70001-1111-2222-3333-444455556666"
        with FixtureEndpoint([row(sid, source="cluster")]) as fx:
            _, rows = self.json_listing(fx.url)
        self.assertEqual([r["source"] for r in rows], ["cluster"])

    def test_json_listing_omits_a_stale_row(self):
        with FixtureEndpoint([row("deadp1d0-1111-2222-3333-444455556666", live=False)]) as fx:
            _, rows = self.json_listing(fx.url)
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
