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


def load_from(directory):
    """Load a COPY of `session-liveness.py` placed alone in `directory`.

    The sibling imports (`live-workers.py`, `session-identity.py`) resolve against the script's
    OWN directory, so a copy in an otherwise empty directory is how a case makes one of them
    unimportable without touching the real tree — the same isolation rule the fixture, registry
    and heartbeat store follow.
    """
    with open(os.path.join(os.path.dirname(_HERE), "session-liveness.py"), encoding="utf-8") as src:
        text = src.read()
    dst = os.path.join(directory, "session-liveness.py")
    with open(dst, "w", encoding="utf-8") as out:
        out.write(text)
    spec = importlib.util.spec_from_file_location("session_liveness_isolated", dst)
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
        # The cluster reachability marker lives in the heartbeat store, and it decides a
        # not-live cluster row's verdict — so it is isolated too: a suite reading the real store
        # would answer for whatever the machine's mirror last wrote. `live-workers.py` resolves
        # this variable, which is exactly the store the marker is read from.
        self.heartbeat = os.path.join(self.tmp.name, "heartbeat")
        os.makedirs(self.heartbeat)
        self._prior_heartbeat_env = os.environ.get("SUPERVISOR_HEARTBEAT_DIR")
        os.environ["SUPERVISOR_HEARTBEAT_DIR"] = self.heartbeat

    def tearDown(self):
        if self._prior_cache_env is None:
            os.environ.pop("SUPERVISOR_START_CACHE", None)
        else:
            os.environ["SUPERVISOR_START_CACHE"] = self._prior_cache_env
        if self._prior_heartbeat_env is None:
            os.environ.pop("SUPERVISOR_HEARTBEAT_DIR", None)
        else:
            os.environ["SUPERVISOR_HEARTBEAT_DIR"] = self._prior_heartbeat_env
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

    def coverage(self, endpoint, directory=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.m.main(
                ["--coverage", "--endpoint", endpoint, "--dir", directory or self.registry]
            )
        return rc, out.getvalue() + err.getvalue()

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

    def test_a_row_with_a_non_string_session_id_is_unknown(self):
        # `by_id` keys on `r["session_id"]` and then calls `.lower()` on it; a non-string id
        # raises `AttributeError`, which without a guard escapes `check()` as exit 1 = ABSENT.
        # The row cannot be compared against the (string) argument, so the answer is UNKNOWN —
        # the same "a store this reader does not understand" rule the non-boolean `live` gets.
        bad = row("12345678-1111-2222-3333-444455556666")
        bad["session_id"] = 12345678
        with FixtureEndpoint([bad]) as fx:
            rc, out = self.check("12345678", fx.url)
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("UNKNOWN", out)

    def test_an_unexpected_exception_is_unknown_not_a_crash(self):
        # The blanket guard on `check()`: an exception `_check` does not itself anticipate (a
        # RuntimeError from the HTTP layer, say — not the `OSError`/`ValueError` it catches) must
        # become UNKNOWN. An uncaught traceback would exit 1, this file's ABSENT code, i.e.
        # permission to resume onto a live conversation.
        def boom(*_a, **_k):
            raise RuntimeError("boom")

        real = self.m._get_json
        self.m._get_json = boom
        try:
            rc, out = self.check("3fd529af", "http://127.0.0.1:1")
        finally:
            self.m._get_json = real
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("UNKNOWN", out)

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

    # ---- the cluster reachability marker (the dropped safety signal) --------------------

    def test_a_not_live_cluster_row_with_a_stale_marker_is_unknown(self):
        # The regression this guards: the row's `state` is the session's ACTIVITY, never whether
        # the cluster was reachable, so the cluster-unreachable fact survives only in the LOCAL
        # marker. A missing marker is stale, and a stale marker means the mirror could not read
        # the cluster — so this row may be a worker alive behind a network fault, and ABSENT
        # would authorise a resume onto it.
        sid = "c1u57e00-1111-2222-3333-444455556666"
        with FixtureEndpoint([row(sid, live=False, source="cluster")]) as fx:
            rc, out = self.check(sid, fx.url)
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("UNKNOWN", out)

    def test_a_not_live_cluster_row_with_a_fresh_marker_is_absent(self):
        # A fresh marker means the mirror DID read the cluster, so the staleness is the ordinary
        # death case and ABSENT is licensed.
        open(os.path.join(self.heartbeat, "_cluster-reachability.json"), "w").close()
        sid = "c1u57e00-1111-2222-3333-444455556666"
        with FixtureEndpoint([row(sid, live=False, source="cluster")]) as fx:
            rc, out = self.check(sid, fx.url)
        self.assertEqual(rc, ABSENT)
        self.assertIn("ABSENT", out)

    def test_a_not_live_non_cluster_row_is_absent_even_with_a_stale_marker(self):
        # The marker is consulted ONLY for a cluster row: a local stale row keeps its ABSENT.
        sid = "10ca1500-1111-2222-3333-444455556666"
        with FixtureEndpoint([row(sid, live=False, source="mcp-timer")]) as fx:
            rc, out = self.check(sid, fx.url)
        self.assertEqual(rc, ABSENT)
        self.assertIn("ABSENT", out)

    def test_a_live_cluster_row_is_live(self):
        # The marker governs only the not-live arm — a live cluster row is LIVE regardless.
        sid = "11vec1u5-1111-2222-3333-444455556666"
        with FixtureEndpoint([row(sid, source="cluster")]) as fx:
            rc, out = self.check(sid, fx.url)
        self.assertEqual(rc, LIVE)

    def test_an_unimportable_live_workers_is_unknown_not_absent(self):
        # `_live_workers()` exec_module's the sibling with no guard, and this runs ON the
        # ABSENT-licensing path (a not-live `source: cluster` row). A missing or unparseable
        # `live-workers.py` must therefore answer UNKNOWN: an escape here is exit 1 = ABSENT,
        # i.e. permission to resume onto a cluster worker that may be alive behind a fault.
        mod = load_from(self.tmp.name)  # the copy sits alone: no sibling live-workers.py
        sid = "c1u57e00-1111-2222-3333-444455556666"
        with FixtureEndpoint([row(sid, live=False, source="cluster")]) as fx:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = mod.main(["--check", sid, "--endpoint", fx.url])
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("UNKNOWN", out.getvalue() + err.getvalue())

    # ---- the id is an argument, and the per-id URL is where it is interpolated -----------

    def test_a_hostile_id_never_reaches_the_wire_raw(self):
        # `quote(sid, safe="")` is the only thing between a hostile argument and a path traversal
        # or a query injection on the per-id route. Pin it: none of `../`, `?`, `%2f` may survive
        # unescaped into a URL the probe builds.
        seen = []
        real = self.m._get_json

        def spy(url, timeout=None):
            seen.append(url)
            return real(url, timeout)

        self.m._get_json = spy
        try:
            with FixtureEndpoint([]) as fx:
                for hostile in ("../etc/passwd", "abc?live=true", "abc%2fdef"):
                    rc, out = self.check(hostile, fx.url)
                    self.assertEqual(rc, ABSENT, hostile)
        finally:
            self.m._get_json = real
        self.assertTrue(seen)
        for url in seen:
            self.assertNotIn("../", url)
            self.assertNotIn("?", url)
            self.assertNotIn("%2f", url)

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

    def test_empty_list_refuses_a_partial_rollout(self):
        # A partial rollout: the endpoint holds no live row while the registry still lists a live
        # session. An empty list here is the confident-empty-fleet shape — `commands/open.md`
        # Step 2C reads this call to decide whether to open a session — so it must not be
        # presented as complete. It pays the same coverage precondition ABSENT pays.
        self.plant("aaaa1111-1111-2222-3333-444455556666")
        with FixtureEndpoint([]) as fx:
            rc, out = self.listing(fx.url)
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("UNKNOWN", out)

    def test_empty_list_is_live_when_the_fleet_is_genuinely_idle(self):
        # Endpoint empty AND registry empty: coverage is complete, so the empty list is honest.
        with FixtureEndpoint([]) as fx:
            rc, rows = self.json_listing(fx.url)
        self.assertEqual(rc, LIVE)
        self.assertEqual(rows, [])

    def test_non_empty_list_refuses_a_partial_rollout(self):
        # The dangerous half of the same case, and the one that used to slip through: the
        # endpoint holds a live row, so the list is NON-empty and was handed back as complete
        # while a registry-live session with no live row was silently dropped. `commands/open.md`
        # Step 2C matches `<topic> Manager` against `name`/`formerNames` from exactly this
        # output and spawns on no match, so the omission is a SECOND manager onto a live topic.
        # A non-empty list is a positive claim about the whole fleet, so it pays the same
        # coverage precondition the empty one pays.
        self.plant("aaaa1111-1111-2222-3333-444455556666")
        self.plant("bbbb2222-1111-2222-3333-444455556666")
        with FixtureEndpoint([row("aaaa1111-1111-2222-3333-444455556666")]) as fx:
            rc, out = self.listing(fx.url)
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("UNKNOWN", out)

    def test_non_empty_list_is_live_when_every_registry_session_is_stamped(self):
        # Coverage complete — every registry-live session has a live row — so the list IS the
        # whole live fleet and is served. Without this the refusal above could be a blanket
        # "never list anything" and still pass.
        sid = "aaaa1111-1111-2222-3333-444455556666"
        self.plant(sid)
        with FixtureEndpoint([row(sid)]) as fx:
            rc, rows = self.json_listing(fx.url)
        self.assertEqual(rc, LIVE)
        self.assertEqual(len(rows), 1)

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

    # ---- --coverage: the ABSENT precondition, measured not assumed ----------------------

    def test_coverage_is_complete_when_every_registry_live_session_is_stamped(self):
        # A planted record with this process's pid is not dead by the registry's own rule
        # (`alive` is `None`, not `False`), so it must be stamped at the endpoint.
        sid = "c0ve2a6e-1111-2222-3333-444455556666"
        self.plant(sid)
        with FixtureEndpoint([row(sid)]) as fx:
            rc, line = self.coverage(fx.url)
        self.assertEqual(rc, LIVE)
        self.assertIn("endpoint_live=1 registry_live=1 missing=0", line)

    def test_coverage_is_incomplete_when_a_registry_live_session_is_unstamped(self):
        # The state in which ABSENT is unsafe: a session the registry holds live with no live
        # endpoint row. Non-zero exit is the whole point.
        self.plant("aaaa1111-1111-2222-3333-444455556666")
        with FixtureEndpoint([row("bbbb2222-1111-2222-3333-444455556666")]) as fx:
            rc, line = self.coverage(fx.url)
        self.assertEqual(rc, ABSENT)
        self.assertIn("missing=1", line)

    def test_coverage_on_an_unreachable_endpoint_is_unknown(self):
        # "Could not measure" is not "measured complete" — never a fabricated 0.
        rc, line = self.coverage(dead_url())
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("UNKNOWN", line)

    def test_coverage_on_an_unreadable_registry_is_unknown(self):
        with FixtureEndpoint([row("aaaa1111-1111-2222-3333-444455556666")]) as fx:
            rc, line = self.coverage(fx.url, directory=os.path.join(self.tmp.name, "nope"))
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("UNKNOWN", line)


if __name__ == "__main__":
    unittest.main()
