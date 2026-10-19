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
import socket
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from endpoint_fixture import FixtureEndpoint, dead_url, ledger_row, row

_HERE = os.path.dirname(os.path.abspath(__file__))

LIVE, ABSENT, UNKNOWN, AMBIGUOUS = 0, 1, 2, 3

# The refusal code for a `--check` argument that could not name a session at all. Pinned here
# rather than imported so the suite fails loudly if the module renumbers it.
MALFORMED = 4


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
        # The spawn ledger decides WHICH sessions the `--list` guard covers (SC11), so it is
        # isolated for the same reason as the other three stores — but since 2026-10-14 it is
        # isolated as ROWS rather than as a directory: `spawn-ledger.py` asks the session-ledger
        # endpoint now, so each case's rows travel into its own `FixtureEndpoint` through
        # `self.ledger_rows` (see `fixture()`). A suite reading the machine's real ledger would
        # let 1900 real spawn records decide a fixture's verdict — the isolation rule unchanged,
        # only its shape.
        self.ledger_rows = []

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

    def ledger_entry(self, session_id, label="synth worker", **extra):
        """A spawn-ledger row — what makes a session one the `--list` guard covers.

        ⚠️ **A registry record alone is NOT enough since SC11 (2026-10-09).** The guard's
        population is `registry_live ∩ spawn ledger`, so a case that wants the guard to fire
        must plant BOTH halves; planting only the registry half is now the case that must
        NOT fire, which is its own test below.

        ⚠️ **A row on this case's own list, not a file.** The ledger was a directory until
        2026-10-14 and this method wrote `<sid>.json` into it; the reader asks the endpoint
        now, so the row is appended to `self.ledger_rows` and `fixture()` serves it. A case
        that still wrote a file would assert against a ledger the reader never opens — and
        would pass on a guard that saw nothing.
        """
        self.ledger_rows.append(ledger_row(session_id, label, **extra))

    def fixture(self, rows=None, **kwargs):
        """This case's endpoint: the heartbeat rows given, plus the ledger rows planted.

        ⚠️ **One constructor for both stores, deliberately.** The two now share a base URL,
        so a case that built its own `FixtureEndpoint` without `ledger_rows` would point the
        ledger reader at an empty store and read the result as "nobody was ever spawned" —
        the confident-empty-fleet answer this whole guard exists to refuse.
        """
        return FixtureEndpoint(rows, ledger_rows=list(self.ledger_rows), **kwargs)

    def check(self, session_id, endpoint, directory=None):
        # ⚠️ **`--dir` is not optional here since the ABSENT-licence guard (2026-10-12).** The
        # verdict path now reads the identity registry on the one path that would otherwise
        # license ABSENT, so a case omitting `--dir` would read the MACHINE's real registry —
        # the same isolation rule `setUp` states for the other three stores, and the way a real
        # session on this host could flip a fixture's verdict. Defaults to the planted registry,
        # exactly as `listing` / `json_listing` / `coverage` already do.
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.m.main(
                ["--check", session_id, "--endpoint", endpoint, "--dir", directory or self.registry]
            )
        return rc, out.getvalue() + err.getvalue()

    def check_streams(self, session_id, endpoint):
        """`check`, but with stdout and stderr kept apart.

        ⚠️ **The split is load-bearing for the malformed cases.** The refusal prints to stderr
        while the `--list` fall-through it replaced printed to **stdout**, so a case that merged
        the two could not tell a roster from a verdict — and `assertNotIn("ABSENT", combined)`
        would pass on a build whose stderr carried exactly the ABSENT it is meant to catch.
        """
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            # `--dir` for the same reason `check` passes it: without it the licence path reads
            # this machine's real registry — present on a workstation, absent on a CI runner,
            # where it reads as unreadable and the verdict becomes UNKNOWN (measured: PR #280's
            # `test` job failed on exactly that while every local run passed).
            rc = self.m.main(["--check", session_id, "--endpoint", endpoint, "--dir", self.registry])
        return rc, out.getvalue(), err.getvalue()

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
        with self.fixture([row(sid)]) as fx:
            full = self.check(sid, fx.url)
            pref = self.check("3fd529af", fx.url)
        self.assertEqual(full[0], LIVE)
        self.assertEqual(pref[0], LIVE)
        self.assertEqual(full[0], pref[0], "prefix verdict must equal full-id verdict")

    def test_absent_id_differs_from_a_resolving_prefix(self):
        # (b) and (c) must NOT agree, or the probe is a constant-return stub.
        sid = "3fd529af-1111-2222-3333-444455556666"
        with self.fixture([row(sid)]) as fx:
            self.assertEqual(self.check("3fd529af", fx.url)[0], LIVE)
            self.assertEqual(self.check("deadbeef", fx.url)[0], ABSENT)

    def test_prefix_match_is_case_insensitive(self):
        with self.fixture([row("ABCDEF12-1111-2222-3333-444455556666")]) as fx:
            self.assertEqual(self.check("abcdef12", fx.url)[0], LIVE)

    # ---- UNKNOWN vs ABSENT: the dangerous direction -----------------------------------

    def test_a_404_is_absent_not_unknown(self):
        # The store positively reporting it holds no such session — a READABLE endpoint that
        # licenses ABSENT.
        # ⚠️ The probe must be a WELL-FORMED id (lowercase alphanumerics). A non-id string is now
        # refused as malformed before any verdict, so it can no longer stand in for "an id that
        # matches nothing" — keeping those two apart is the point of the shape guard.
        with self.fixture([]) as fx:
            rc, out = self.check("doesnotexist", fx.url)
        self.assertEqual(rc, ABSENT)
        self.assertIn("ABSENT", out)

    def test_unreachable_endpoint_is_unknown_not_absent(self):
        # The SC4 direction. A dead socket means the probe could not run, and folding that into
        # "not live" is permission to resume onto a live conversation.
        rc, out = self.check("3fd529af", dead_url())
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("UNKNOWN", out)

    def test_an_unreachable_endpoint_names_the_cause_and_elapsed_ms(self):
        # ⚠️ UNKNOWN was the whole answer until 2026-10-19, so a refused socket, a timeout and an
        # unparseable body were indistinguishable. The refusal now names the cause — the operator
        # reads this line, and the next occurrence has to be diagnosable from it.
        rc, out = self.check("3fd529af", dead_url())
        self.assertEqual(rc, UNKNOWN)
        self.assertRegex(out, r"session-heartbeat endpoint unreachable at .* after \d+ ms \(ECONNREFUSED")

    def test_a_slow_endpoint_is_logged_as_a_timeout_with_elapsed_ms(self):
        # ⚠️ The timeout is raised through a patched `urlopen`, NOT a real slow stub. A real
        # 0.2 s socket timeout against a 1 s stub was observed returning the body on a loaded
        # host — the case then passes or fails by machine load, which is the same class of
        # flakiness the fixture isolation exists to remove. The real socket timeout is exercised
        # by the manual forced-failure run against an 8 s stub (`# Progress`, 5019 ms).
        messages = []
        err = io.StringIO()
        with mock.patch.object(
            self.m.urllib.request, "urlopen", side_effect=socket.timeout("timed out")
        ):
            with redirect_stderr(err):
                rows = self.m.read_endpoint("http://fixture.invalid", on_failure=messages.append)
        self.assertIsNone(rows)
        self.assertRegex(err.getvalue(), r"session-heartbeat read failed after \d+ ms: timeout")
        self.assertEqual(len(messages), 1, "the observer must receive the same line the log got")

    def test_an_unparseable_body_is_logged_as_unparseable(self):
        with self.fixture([], list_raw="<html>not json</html>") as fx:
            err = io.StringIO()
            with redirect_stderr(err):
                rows = self.m.read_endpoint(fx.url)
        self.assertIsNone(rows)
        self.assertRegex(err.getvalue(), r"unparseable body")

    def test_a_non_200_is_logged_with_its_status(self):
        with self.fixture([], list_status=500) as fx:
            err = io.StringIO()
            with redirect_stderr(err):
                rows = self.m.read_endpoint(fx.url)
        self.assertIsNone(rows)
        self.assertRegex(err.getvalue(), r"HTTP 500")

    def test_a_non_200_non_404_status_is_unknown_not_absent(self):
        # A 500 from the list route (reached when the per-id route answered 404) is not the
        # store saying "gone" — it is the store failing, which is UNKNOWN.
        with self.fixture([], list_status=500) as fx:
            rc, out = self.check("3fd529af", fx.url)
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("UNKNOWN", out)

    def test_a_stale_row_is_absent_not_live(self):
        # A row the store holds but marks not live — positively reported, so ABSENT is licensed.
        sid = "57a1e111-1111-2222-3333-444455556666"
        with self.fixture([row(sid, live=False)]) as fx:
            rc, out = self.check(sid, fx.url)
        self.assertEqual(rc, ABSENT)
        self.assertIn("ABSENT", out)

    # ---- the malformed argument: a value that could not name a session is never a verdict ---

    def test_a_multi_id_blob_is_malformed_not_absent(self):
        # The incident this guard exists for. A shell loop expanded a newline-separated id string
        # unquoted; under zsh that is not word-split, so the whole blob arrived as ONE argument
        # and the probe answered ABSENT — the one verdict that authorises a resume onto a session
        # that is already running. No endpoint is needed: the refusal precedes every HTTP read.
        rc, out, err = self.check_streams("abc12345\nabc12345", dead_url())
        self.assertEqual(rc, MALFORMED)
        self.assertIn("malformed", err)
        self.assertNotIn("ABSENT", out)
        self.assertNotIn("ABSENT", err)

    def test_a_non_id_string_is_malformed(self):
        # ⚠️ The clause a whitespace-only guard would miss: this value carries no whitespace at
        # all, so only the hyphen-position rule rejects it. Without this case, a guard that
        # tested `" " in arg or "\n" in arg` would tick every other malformed probe while this
        # one still read ABSENT/exit 1.
        rc, out, err = self.check_streams("not-an-id-at-all", dead_url())
        self.assertEqual(rc, MALFORMED)
        self.assertNotIn("ABSENT", out + err)

    def test_a_whitespace_only_argument_is_malformed_not_empty(self):
        # ⚠️ Deliberately NOT the empty case below. `_check` strips before its own emptiness
        # test, so an implementation that stripped first would fold the two together and answer
        # UNKNOWN — which is exactly why the guard runs on the RAW argument.
        rc, out, err = self.check_streams("   ", dead_url())
        self.assertEqual(rc, MALFORMED)
        self.assertNotIn("ABSENT", out + err)

    def test_an_argument_with_internal_whitespace_is_malformed(self):
        rc, _, err = self.check_streams("abc 12345", dead_url())
        self.assertEqual(rc, MALFORMED)
        self.assertNotIn("ABSENT", err)

    def test_an_overlong_argument_is_malformed(self):
        # The ≤36 cap is part of the rule; without this case a guard that drops it still passes.
        rc, _, err = self.check_streams("a" * 37, dead_url())
        self.assertEqual(rc, MALFORMED)
        self.assertNotIn("ABSENT", err)

    def test_an_empty_argument_is_unknown_and_prints_no_roster(self):
        # ⚠️ The falsy-guard half of the fix. `if args.check:` sent `""` past the branch and into
        # the `--list` fall-through, so an empty argument silently changed the command's MODE and
        # a caller reading the roster as a verdict saw a non-empty result.
        with self.fixture([row("3fd529af-1111-2222-3333-444455556666")]) as fx:
            rc, out, err = self.check_streams("", fx.url)
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("no session id given", err)
        self.assertEqual(out, "", "an empty id must not fall through to the listing")

    def test_a_well_formed_non_matching_id_is_still_absent(self):
        # The unchanged control: the guard rejects non-ids, never ids that simply match nothing.
        with self.fixture([]) as fx:
            rc, out, err = self.check_streams("zzzzzzzz", fx.url)
        self.assertEqual(rc, ABSENT)
        self.assertIn("ABSENT — no session id matches zzzzzzzz", err)
        self.assertEqual(out, "")

    def test_the_ambiguous_recovery_prefix_is_not_malformed(self):
        # ⚠️ **The regression guard for the tightening direction.** `_check` answers a collision
        # with `AMBIGUOUS — … pass a longer id: <m[:12]>`, so a 12-char prefix of a HYPHENATED id
        # — which carries a hyphen at offset 8 — is the instrument's own recovery input. A rule
        # demanding exactly 8 characters would refuse the instruction it had just printed, and
        # would narrow `prefix matching` that the verdict machinery is documented to leave alone.
        sid = "3fd529af-1111-2222-3333-444455556666"
        with self.fixture([row(sid)]) as fx:
            rc, _, _ = self.check_streams("3fd529af-111", fx.url)
        self.assertNotEqual(rc, MALFORMED)
        self.assertEqual(rc, LIVE)

    def test_a_longer_prefix_of_a_real_id_is_not_malformed(self):
        # Any prefix length resolves, not only 8 and not only the UUID's own separators.
        sid = "3fd529af-1111-2222-3333-444455556666"
        with self.fixture([row(sid)]) as fx:
            rc, _, _ = self.check_streams("3fd529af-1111-2222", fx.url)
        self.assertEqual(rc, LIVE)

    def test_an_uppercase_id_is_not_malformed(self):
        # `_check` folds case, so the guard must fold too — a literal-lowercase rule would turn
        # an id that resolves today into an input error.
        sid = "abcdef12-1111-2222-3333-444455556666"
        with self.fixture([row(sid)]) as fx:
            rc, _, _ = self.check_streams("ABCDEF12", fx.url)
        self.assertEqual(rc, LIVE)

    def test_a_list_is_unaffected_by_the_guard(self):
        # The branch the empty case used to fall through to still works when it is ASKED for.
        sid = "3fd529af-1111-2222-3333-444455556666"
        with self.fixture([row(sid)]) as fx:
            rc, out = self.listing(fx.url)
        self.assertEqual(rc, LIVE)
        self.assertIn("3fd529af", out)

    def test_a_row_without_a_live_flag_is_unknown(self):
        # The store always sends `live`; a row that carries something else is a store this
        # reader does not understand, and "I do not understand this row" is never ABSENT.
        sid = "n0f1a600-1111-2222-3333-444455556666"
        bad = row(sid)
        bad["live"] = None
        with self.fixture([bad]) as fx:
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
        with self.fixture([bad]) as fx:
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
        with self.fixture(self.colliding_pair()) as fx:
            rc, out = self.check("abc12345", fx.url)
        self.assertEqual(rc, AMBIGUOUS)
        self.assertIn("pass a longer id", out)

    def test_ambiguous_candidates_are_distinguishable(self):
        # Echoing 8 chars back hands the caller two identical strings and no way to choose.
        with self.fixture(self.colliding_pair()) as fx:
            _, out = self.check("abc12345", fx.url)
        self.assertIn("abc12345-111", out)
        self.assertIn("abc12345-999", out)

    def test_a_longer_prefix_disambiguates(self):
        with self.fixture(self.colliding_pair()) as fx:
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
        with self.fixture([row(sid, live=False, source="cluster")]) as fx:
            rc, out = self.check(sid, fx.url)
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("UNKNOWN", out)

    def test_a_not_live_cluster_row_with_a_fresh_marker_is_absent(self):
        # A fresh marker means the mirror DID read the cluster, so the staleness is the ordinary
        # death case and ABSENT is licensed.
        open(os.path.join(self.heartbeat, "_cluster-reachability.json"), "w").close()
        sid = "c1u57e00-1111-2222-3333-444455556666"
        with self.fixture([row(sid, live=False, source="cluster")]) as fx:
            rc, out = self.check(sid, fx.url)
        self.assertEqual(rc, ABSENT)
        self.assertIn("ABSENT", out)

    def test_a_not_live_non_cluster_row_is_absent_even_with_a_stale_marker(self):
        # The marker is consulted ONLY for a cluster row: a local stale row keeps its ABSENT.
        sid = "10ca1500-1111-2222-3333-444455556666"
        with self.fixture([row(sid, live=False, source="mcp-timer")]) as fx:
            rc, out = self.check(sid, fx.url)
        self.assertEqual(rc, ABSENT)
        self.assertIn("ABSENT", out)

    def test_a_live_cluster_row_is_live(self):
        # The marker governs only the not-live arm — a live cluster row is LIVE regardless.
        sid = "11vec1u5-1111-2222-3333-444455556666"
        with self.fixture([row(sid, source="cluster")]) as fx:
            rc, out = self.check(sid, fx.url)
        self.assertEqual(rc, LIVE)

    def test_an_unimportable_live_workers_is_unknown_not_absent(self):
        # `_live_workers()` exec_module's the sibling with no guard, and this runs ON the
        # ABSENT-licensing path (a not-live `source: cluster` row). A missing or unparseable
        # `live-workers.py` must therefore answer UNKNOWN: an escape here is exit 1 = ABSENT,
        # i.e. permission to resume onto a cluster worker that may be alive behind a fault.
        mod = load_from(self.tmp.name)  # the copy sits alone: no sibling live-workers.py
        sid = "c1u57e00-1111-2222-3333-444455556666"
        with self.fixture([row(sid, live=False, source="cluster")]) as fx:
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
        #
        # ⚠️ **Two layers, and the case asserts both.** `main()`'s shape guard refuses these at
        # the argument boundary, so they can no longer reach the wire *through `main()`* — but
        # the encoding guarantee belongs to `_check` and must still hold for any other caller,
        # so it is pinned where it lives rather than inferred from the guard. Asserting only the
        # refusal would retire this coverage silently; asserting only `_check` would miss that
        # the boundary now rejects them.
        seen = []
        real = self.m._get_json

        def spy(url, timeout=None):
            seen.append(url)
            return real(url, timeout)

        self.m._get_json = spy
        try:
            with self.fixture([]) as fx:
                for hostile in ("../etc/passwd", "abc?live=true", "abc%2fdef"):
                    # Layer 1 — the argument boundary refuses it, and never as a verdict.
                    rc_main, _, err = self.check_streams(hostile, fx.url)
                    self.assertEqual(rc_main, MALFORMED, hostile)
                    self.assertNotIn("ABSENT", err)
                    # Layer 2 — reached directly, `_check` still encodes it.
                    rc, _, _ = self.m._check(hostile, fx.url, registry_dir=self.registry)
                    self.assertEqual(rc, ABSENT, hostile)
        finally:
            self.m._get_json = real
        self.assertTrue(seen)
        for url in seen:
            self.assertNotIn("../", url)
            self.assertNotIn("?", url)
            self.assertNotIn("%2f", url)

    # ---- --list ------------------------------------------------------------------------

    def test_list_carries_every_registry_live_session_and_marks_the_unstamped(self):
        # ⚠️ **Both registry-live sessions are listed; the one the endpoint holds no `live: true`
        # row for is DEGRADED rather than dropped.** Dropping it is the 2026-09-18 duplicate-spawn
        # shape (`commands/open.md` Step 2C matches a roster row against exactly this output), and
        # refusing the whole list is the 2026-10-18 outage this change removes.
        self.plant("aaaa1111-1111-2222-3333-444455556666", name="Alive")
        self.plant("bbbb2222-1111-2222-3333-444455556666", name="Dead")
        rows = [
            row("aaaa1111-1111-2222-3333-444455556666"),
            row("bbbb2222-1111-2222-3333-444455556666", live=False),
        ]
        with self.fixture(rows) as fx:
            rc, out = self.listing(fx.url)
        self.assertEqual(rc, LIVE)
        self.assertIn("Alive", out)
        self.assertIn("Dead", out)
        self.assertIn("DEGRADED", out)

    def test_list_prints_the_counts_summary(self):
        """The `rows=<n> registry_live=<n> degraded=<n>` line the acceptance criterion reads.

        It is what makes the criterion mechanical: `rows` must equal `registry_live` (a smaller
        `rows` means a session was DROPPED rather than marked), and `degraded` must be non-zero
        exactly when a registry-live session holds no `live: true` row — so a fix that emits
        plain rows with no marker reads `degraded=0` and fails.
        """
        self.plant("aaaa1111-1111-2222-3333-444455556666")
        self.plant("bbbb2222-1111-2222-3333-444455556666")
        with self.fixture([row("aaaa1111-1111-2222-3333-444455556666")]) as fx:
            rc, out = self.listing(fx.url)
        self.assertEqual(rc, LIVE)
        self.assertIn("rows=2 registry_live=2 degraded=1", out)

    def test_list_includes_a_row_with_no_identity_entry(self):
        # A headless or cluster worker holds a heartbeat row and no registry entry; it must
        # still appear, with an empty name rather than being dropped.
        sid = "11571e57-1111-2222-3333-444455556666"
        with self.fixture([row(sid)]) as fx:
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
        with self.fixture([row("aaaa1111-1111-2222-3333-444455556666")]) as fx:
            rc, out = self.listing(fx.url, directory=os.path.join(self.tmp.name, "does-not-exist"))
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("UNKNOWN", out)

    def test_empty_endpoint_still_lists_a_registry_live_session_as_degraded(self):
        # A partial rollout: the endpoint holds no live row while the registry still lists a live
        # session. The list must not read as a confident empty fleet — `commands/open.md` Step 2C
        # reads this call to decide whether to open a session — but it must not refuse either:
        # the session is carried with `degraded: true`, so Step 2C still sees it.
        self.plant("aaaa1111-1111-2222-3333-444455556666")
        self.ledger_entry("aaaa1111-1111-2222-3333-444455556666")
        with self.fixture([]) as fx:
            rc, rows = self.json_listing(fx.url)
        self.assertEqual(rc, LIVE)
        self.assertEqual([r["sessionId"] for r in rows],
                         ["aaaa1111-1111-2222-3333-444455556666"])
        self.assertTrue(rows[0]["degraded"])

    def test_empty_list_is_live_when_the_fleet_is_genuinely_idle(self):
        # Endpoint empty AND registry empty: coverage is complete, so the empty list is honest.
        with self.fixture([]) as fx:
            rc, rows = self.json_listing(fx.url)
        self.assertEqual(rc, LIVE)
        self.assertEqual(rows, [])

    def test_non_empty_list_degrades_the_unstamped_session(self):
        # The dangerous half of the same case, and the one that used to slip through: the
        # endpoint holds a live row, so the list is NON-empty and was handed back as complete
        # while a registry-live session with no live row was silently dropped. `commands/open.md`
        # Step 2C matches `<topic> Manager` against `name`/`formerNames` from exactly this
        # output and spawns on no match, so the omission is a SECOND manager onto a live topic.
        # A non-empty list is a positive claim about the whole fleet, so it pays the same
        # coverage precondition the empty one pays.
        self.plant("aaaa1111-1111-2222-3333-444455556666")
        self.plant("bbbb2222-1111-2222-3333-444455556666")
        self.ledger_entry("aaaa1111-1111-2222-3333-444455556666")
        self.ledger_entry("bbbb2222-1111-2222-3333-444455556666")
        with self.fixture([row("aaaa1111-1111-2222-3333-444455556666")]) as fx:
            rc, rows = self.json_listing(fx.url)
        self.assertEqual(rc, LIVE)
        by_id = {r["sessionId"]: r for r in rows}
        self.assertFalse(by_id["aaaa1111-1111-2222-3333-444455556666"]["degraded"])
        self.assertTrue(by_id["bbbb2222-1111-2222-3333-444455556666"]["degraded"])

    def test_a_registry_live_session_outside_the_ledger_is_listed_and_degraded(self):
        """SC11's narrowing — the reason the guard was realigned on 2026-10-09.

        A manager, a bot and the operator's own sessions are registry-live and hold **no
        ledger record**, because they were not opened by the supervisor and never load the
        plugin that stamps. Measured live before the narrowing: `unstamped=11 registry_live=27`,
        with 9 of the 11 in exactly this class — so the guard refused on sessions that cannot
        stamp by construction, and `approved-not-started.py` rendered `unknown` behind it.
        Here the registry-live session with no row and no ledger record must NOT withhold the
        list; the live one is served.
        """
        sid = "aaaa1111-1111-2222-3333-444455556666"
        other = "bbbb2222-1111-2222-3333-444455556666"
        self.plant(other, name="Fleet Manager")
        with self.fixture([row(sid)]) as fx:
            rc, rows = self.json_listing(fx.url)
        self.assertEqual(rc, LIVE)
        by_id = {r["sessionId"]: r for r in rows}
        self.assertEqual(sorted(by_id), sorted([sid, other]))
        self.assertFalse(by_id[sid]["degraded"])
        self.assertTrue(by_id[other]["degraded"])

    def test_a_ledger_worker_with_no_row_is_degraded_not_withheld(self):
        """The guard's finding rides ON the row instead of refusing the whole list.

        A ledger record plus a live registry entry plus no endpoint row is precisely the rollout
        gap the guard exists for — so it must still be visible. It is visible as `degraded: true`
        rather than by withholding every other session: measured live 2026-10-18, refusing the
        list on twelve stale-plugin sessions left the fleet unlistable AND blocked
        `plugin-version-census.py`, the one tool that names them.
        """
        sid = "aaaa1111-1111-2222-3333-444455556666"
        self.plant(sid, name="Stamping Worker")
        self.ledger_entry(sid)
        with self.fixture([]) as fx:
            rc, rows = self.json_listing(fx.url)
        self.assertEqual(rc, LIVE)
        self.assertEqual([r["sessionId"] for r in rows], [sid])
        self.assertTrue(rows[0]["degraded"])

    def test_an_unreadable_ledger_is_unknown_not_a_pass(self):
        """`{}` and None are different answers, and only one of them may satisfy the guard.

        A ledger that cannot be read must not narrow the population to nothing and hand back
        a passing precondition — that is the resume-authorising direction, and it is the same
        collapse the endpoint and registry reads already refuse.
        """
        self.plant("aaaa1111-1111-2222-3333-444455556666")
        # ⚠️ **A non-200 on the ledger route, not a chmodded path.** The old spelling made a
        # directory unreadable and relied on `os.listdir` raising; the store's equivalent is a
        # route answering something other than 200, which `spawn-ledger.py` turns into `None`.
        # The distinction from an empty store is the whole contract: a 500 REFUSES, `200 []`
        # permits.
        with self.fixture(
            [row("bbbb2222-1111-2222-3333-444455556666")], ledger_status=500
        ) as fx:
            rc, out = self.listing(fx.url)
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("UNKNOWN", out)

    def test_an_unimportable_spawn_ledger_is_unknown_not_a_crash(self):
        """`_spawn_ledger()` exec_module's its sibling with no guard, and the guard it feeds
        is the one that LICENSES the list. An escape here would surface as exit 1 — this
        file's ABSENT code, which every caller reads as "not live, resume is authorised" —
        so a missing `spawn-ledger.py` must answer UNKNOWN, the same rule `live-workers.py`
        already carries on the cluster path.
        """
        alone = os.path.join(self.tmp.name, "no-ledger-module")
        os.makedirs(alone)
        # `session-identity.py` is present so the failure is specifically the ledger sibling,
        # not whichever import happens to run first.
        with open(os.path.join(os.path.dirname(_HERE), "session-identity.py"), encoding="utf-8") as src:
            text = src.read()
        with open(os.path.join(alone, "session-identity.py"), "w", encoding="utf-8") as out:
            out.write(text)
        mod = load_from(alone)
        self.plant("aaaa1111-1111-2222-3333-444455556666")
        with self.fixture([row("aaaa1111-1111-2222-3333-444455556666")]) as fx:
            out_s, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out_s), redirect_stderr(err):
                rc = mod.main(["--list", "--endpoint", fx.url, "--dir", self.registry])
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("UNKNOWN", out_s.getvalue() + err.getvalue())

    def test_non_empty_list_is_live_when_every_registry_session_is_stamped(self):
        # Coverage complete — every registry-live session has a live row — so the list IS the
        # whole live fleet and is served. Without this the refusal above could be a blanket
        # "never list anything" and still pass.
        sid = "aaaa1111-1111-2222-3333-444455556666"
        self.plant(sid)
        with self.fixture([row(sid)]) as fx:
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
        with self.fixture([row(sid)]) as fx:
            rc, rows = self.json_listing(fx.url)
        self.assertEqual(rc, LIVE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Renamed Topic Manager")
        self.assertEqual(rows[0]["formerNames"], ["Topic Manager"])
        self.assertEqual(rows[0]["cwd"], "/Users/bborbe/Documents/workspaces/thing")
        self.assertEqual(rows[0]["nameSource"], "user")

    def test_json_listing_marks_the_source(self):
        sid = "bea70001-1111-2222-3333-444455556666"
        with self.fixture([row(sid, source="cluster")]) as fx:
            _, rows = self.json_listing(fx.url)
        self.assertEqual([r["source"] for r in rows], ["cluster"])

    def test_json_listing_omits_a_stale_row(self):
        with self.fixture([row("deadp1d0-1111-2222-3333-444455556666", live=False)]) as fx:
            _, rows = self.json_listing(fx.url)
        self.assertEqual(rows, [])

    # ---- --coverage: the ABSENT precondition, measured not assumed ----------------------

    def test_coverage_is_complete_when_every_registry_live_session_is_stamped(self):
        # A planted record with this process's pid is not dead by the registry's own rule
        # (`alive` is `None`, not `False`), so it must be stamped at the endpoint.
        sid = "c0ve2a6e-1111-2222-3333-444455556666"
        self.plant(sid)
        with self.fixture([row(sid)]) as fx:
            rc, line = self.coverage(fx.url)
        self.assertEqual(rc, LIVE)
        self.assertIn("endpoint_live=1 registry_live=1 missing=0", line)

    def test_coverage_is_incomplete_when_a_registry_live_session_is_unstamped(self):
        # The state in which ABSENT is unsafe: a session the registry holds live with no live
        # endpoint row. Non-zero exit is the whole point.
        self.plant("aaaa1111-1111-2222-3333-444455556666")
        with self.fixture([row("bbbb2222-1111-2222-3333-444455556666")]) as fx:
            rc, line = self.coverage(fx.url)
        self.assertEqual(rc, ABSENT)
        self.assertIn("missing=1", line)

    def test_coverage_on_an_unreachable_endpoint_is_unknown(self):
        # "Could not measure" is not "measured complete" — never a fabricated 0.
        rc, line = self.coverage(dead_url())
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("UNKNOWN", line)

    def test_coverage_on_an_unreadable_registry_is_unknown(self):
        with self.fixture([row("aaaa1111-1111-2222-3333-444455556666")]) as fx:
            rc, line = self.coverage(fx.url, directory=os.path.join(self.tmp.name, "nope"))
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("UNKNOWN", line)

    # ---- the ABSENT licence: the registry's coverage precondition (2026-10-12) -------------
    #
    # The 2026-10-10 incident: `--check` returned a decisive ABSENT (exit 1) for a session that
    # was demonstrably live on three independent signals. The verdict path licensed ABSENT from
    # a readable endpoint alone, while the `--list` path already paid the coverage precondition
    # (`unstamped()`). These cases pin the licence and — as importantly — the two things it must
    # NOT swallow: the genuinely-dead id, and the endpoint's own positive `live: false`.

    def test_a_registry_live_id_with_no_endpoint_row_is_unknown_not_absent(self):
        # SC1. The exact shape: the registry holds the id as non-dead while the endpoint holds NO
        # row for it. "The endpoint has never heard of it" is a fact about the endpoint's
        # COVERAGE, not about the session, and ABSENT is the one verdict that authorises a resume.
        # Pre-fix this answered ABSENT — the message the incident recorded verbatim.
        sid = "bf00c554-b829-44eb-813b-437fe2f45322"
        self.plant(sid)
        with self.fixture([]) as fx:
            rc, out = self.check(sid, fx.url)
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("coverage of live sessions is", out)

    def test_an_unmatched_id_keeps_its_absent_even_when_the_registry_is_populated(self):
        # SC2's control, and the half a careless fix loses: a POPULATED registry with no match
        # for the argument is still the genuine ABSENT. A guard that answered UNKNOWN for every
        # unmatched id would erase the death verdict the whole instrument exists to give — the
        # failure mode in the opposite direction from the incident.
        self.plant("aaaa1111-1111-2222-3333-444455556666")
        with self.fixture([]) as fx:
            # ⚠️ **A WELL-FORMED id that matches nothing — never a prose string.** Since #277
            # (`is_session_id`), a value with no session-id shape is refused as `MALFORMED`
            # (exit 4) at the argument boundary and never reaches the licence path at all, so
            # `does-not-exist-xyz` would assert the wrong layer. Master's own 404 case made the
            # same correction; this is that rule applied to a case added in the same PR.
            rc, out = self.check("deadbeef-1111-2222-3333-444455556666", fx.url)
        self.assertEqual(rc, ABSENT)
        self.assertIn("no session id matches", out)

    def test_an_endpoint_row_reporting_not_live_is_absent_even_when_the_registry_holds_it(self):
        # SC2. Here the endpoint POSITIVELY answered about this id — `live: false` is a dead
        # session the store can see and is reporting on, not a rollout gap. The guard is scoped
        # to the no-row case, so this keeps its ABSENT even with a registry record present; a
        # fix that widened to every ABSENT would lose the death verdict.
        sid = "57a1e111-1111-2222-3333-444455556666"
        self.plant(sid)
        with self.fixture([row(sid, live=False)]) as fx:
            rc, out = self.check(sid, fx.url)
        self.assertEqual(rc, ABSENT)

    def test_an_unreadable_registry_is_unknown_not_absent(self):
        # "Could not measure the precondition" is not "the precondition holds". `read_registry`
        # returns `None` for a missing directory — distinct from `{}` — and folding it into
        # ABSENT re-opens the hole on a host where the registry is unreadable rather than merely
        # incomplete. Same rule `coverage()` carries at exit 2.
        with self.fixture([]) as fx:
            # ⚠️ Well-formed id, for the reason stated on the unmatched-id control above: a prose
            # string exits 4 at the argument boundary and never reaches the registry read this
            # case is about.
            rc, out = self.check(
                "deadbeef-1111-2222-3333-444455556666", fx.url,
                directory=os.path.join(self.tmp.name, "nope"),
            )
        self.assertEqual(rc, UNKNOWN)
        self.assertIn("identity registry could not be read", out)

    def test_a_prefix_of_a_registry_live_id_is_unknown_not_absent(self):
        # The argument is a prefix as often as a full id (`[ref]` is 6 chars, `sid8` is 8), and
        # `read_registry` is keyed by the FULL id only — so the guard resolves a prefix the same
        # way `_check` resolves one against the endpoint. Without that, a prefixed probe keeps
        # the false ABSENT the guard exists to remove.
        sid = "bf00c554-b829-44eb-813b-437fe2f45322"
        self.plant(sid)
        with self.fixture([]) as fx:
            rc, out = self.check("bf00c554", fx.url)
        self.assertEqual(rc, UNKNOWN)

    def test_coverage_and_check_agree_on_the_incomplete_coverage_fixture(self):
        # SC3. One predicate, two readers: `--coverage` reports `missing >= 1` in exactly the
        # state `--check` refuses to call ABSENT, so the measurement and the verdict rest on one
        # rule and cannot disagree about the same fixture.
        sid = "bf00c554-b829-44eb-813b-437fe2f45322"
        self.plant(sid)
        with self.fixture([]) as fx:
            cov_rc, line = self.coverage(fx.url)
            chk_rc, _ = self.check(sid, fx.url)
        self.assertEqual(cov_rc, ABSENT)
        self.assertIn("missing=1", line)
        self.assertEqual(chk_rc, UNKNOWN)


if __name__ == "__main__":
    unittest.main()
