#!/usr/bin/env python3
"""Tests for scripts/asked-ledger.py.

Covers the property repair C exists to create -- a blocked session is asked
EXACTLY ONCE across layers -- and the one rule that keeps a shared ledger from
re-introducing the per-layer bug it replaced.

The defect being closed, measured twice on 2026-09-20 (`notify-gate.py` records
the same two duplicates from the gate side): `fleet-loop.md` drops an entry whose
owning manager is live, and `manager-loop.md` batches its own topic's set. Each
is correct alone; together they still let the operator receive the SAME question
twice, because a manager and the fleet can both hold the same underlying blocked
session and neither sees the other's batch.

Negative controls are here for the same reason they are in test_open-items: a
check that flags everything satisfies the positive case while catching nothing.
The same asker re-claiming must NOT be refused (that is its own cadence), a
resolved subject must be claimable again (or a re-blocked session deadlocks),
and a corrupt ledger must read empty rather than raise.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import datetime
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "asked-ledger.py")

_spec = importlib.util.spec_from_file_location("asked_ledger", _SCRIPT)
al = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(al)

ASKER = "asker-session-1"
OTHER = "asker-session-2"
SUBJECT = "blocked-session-a"
ROW = "Some Task Row"


def ts(hours_ago):
    when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # LEDGER and LOCK are redirected so no test can touch the real ledger.
        # LOCK is derived from LEDGER at import, so both must move together.
        self._ledger, self._lock = al.LEDGER, al.LOCK
        self._registry = al.REGISTRY
        al.LEDGER = os.path.join(self.tmp, "asked-ledger.json")
        al.LOCK = al.LEDGER + ".lock"
        al.REGISTRY = os.path.join(self.tmp, "sessions")
        os.makedirs(al.REGISTRY)
        self.addCleanup(self._restore)

    def _restore(self):
        al.LEDGER, al.LOCK, al.REGISTRY = self._ledger, self._lock, self._registry

    def _run(self, argv, env_asker=True):
        out, err = io.StringIO(), io.StringIO()
        saved = sys.argv
        saved_env = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        saved_legacy = os.environ.pop("CLAUDE_SESSION_ID", None)
        if env_asker:
            os.environ["CLAUDE_CODE_SESSION_ID"] = ASKER
        sys.argv = ["asked-ledger.py"] + argv
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = al.main()
                except SystemExit as exc:
                    code = exc.code
                    # `sys.exit("msg")` does NOT print where it is raised -- the
                    # interpreter prints the string at shutdown, once SystemExit
                    # has propagated out of main(). Catching it here would swallow
                    # a message the real CLI emits, so the harness reproduces that
                    # final step rather than asserting on a weaker signal.
                    if isinstance(code, str):
                        err.write(code + "\n")
                        code = 1
        finally:
            sys.argv = saved
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            if saved_env is not None:
                os.environ["CLAUDE_CODE_SESSION_ID"] = saved_env
            if saved_legacy is not None:
                os.environ["CLAUDE_SESSION_ID"] = saved_legacy
        return code, out.getvalue(), err.getvalue()

    def claim(self, session=SUBJECT, layer="fleet", asker=ASKER, text="q"):
        argv = ["claim", "--session", session, "--layer", layer, "--text", text]
        if asker is not None:
            argv += ["--asker", asker]
        return self._run(argv)

    def resolve(self, session=SUBJECT, asker=ASKER):
        return self._run(["resolve", "--session", session, "--asker", asker])

    def claim_row(self, row=ROW, layer="worker", asker=ASKER, text="q"):
        argv = ["claim", "--row", row, "--layer", layer, "--text", text]
        if asker is not None:
            argv += ["--asker", asker]
        return self._run(argv)

    def resolve_row(self, row=ROW, asker=ASKER):
        return self._run(["resolve", "--row", row, "--asker", asker])

    def listing(self):
        return self._run(["list"])

    def prune(self, *extra):
        return self._run(["prune", "--no-liveness"] + list(extra))

    def raw(self):
        with open(al.LEDGER, encoding="utf-8") as handle:
            return json.load(handle)

    def backdate(self, session, hours_ago, state="open"):
        """Rewrite asked_at in place. The CLI always stamps 'now', so the only way
        to exercise prune's age branch is to age the entry after the fact."""
        data = self.raw()
        data["entries"][session]["asked_at"] = ts(hours_ago)
        data["entries"][session]["state"] = state
        with open(al.LEDGER, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def live(self, session):
        """Register a session as live, the way ~/.claude/sessions/*.json does."""
        path = os.path.join(al.REGISTRY, "%s.json" % session)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"sessionId": session, "status": "idle"}, handle)


class ExactlyOnceAcrossLayers(Base):
    """The property repair C creates. Without it the operator answers twice."""

    def test_a_second_layer_is_refused_the_same_subject(self):
        code, out, _ = self.claim(layer="fleet", asker=ASKER)
        self.assertEqual(code, 0)
        self.assertIn("claimed", out)

        code, out, _ = self.claim(layer="worker", asker=OTHER)
        self.assertEqual(code, al.HELD, "a second asker must not take the same subject")
        self.assertIn("held by", out)
        self.assertIn(ASKER, out)

    def test_the_refused_subject_is_still_visible_to_the_operator(self):
        """Refused from a batch is not dropped from the world: the consolidated
        list is what makes one ask cover every blocked session."""
        self.claim(layer="fleet", asker=ASKER)
        self.claim(layer="worker", asker=OTHER)
        code, out, _ = self.listing()
        self.assertEqual(code, 0)
        self.assertIn(SUBJECT, out)
        self.assertIn("fleet", out)

    def test_the_same_asker_reclaiming_is_not_a_duplicate(self):
        """Negative control. A layer re-raising its own ask on its own cadence is
        not the cross-layer duplicate, and refusing it would deadlock the asker."""
        self.assertEqual(self.claim(layer="fleet", asker=ASKER)[0], 0)
        code, out, _ = self.claim(layer="fleet", asker=ASKER)
        self.assertEqual(code, 0)
        self.assertIn("reclaimed", out)

    def test_distinct_subjects_are_each_claimed(self):
        """Negative control. The gate is per-subject, not a global lock."""
        self.assertEqual(self.claim(session="s-a", asker=ASKER)[0], 0)
        self.assertEqual(self.claim(session="s-b", asker=OTHER, layer="worker")[0], 0)

    def test_a_resolved_subject_is_claimable_again(self):
        """Negative control. A session that unblocks and blocks again must not be
        deadlocked by its own earlier mark."""
        self.claim(asker=ASKER)
        self.resolve(asker=ASKER)
        code, out, _ = self.claim(asker=OTHER, layer="worker")
        self.assertEqual(code, 0)
        self.assertIn("reopened", out)

    def test_resolve_records_a_resolver_who_is_not_the_asker(self):
        """The operator may answer a subject the fleet raised; the mark is about
        the subject, so the resolver is recorded rather than refused."""
        self.claim(asker=ASKER)
        code, _, _ = self.resolve(asker=OTHER)
        self.assertEqual(code, 0)
        self.assertEqual(self.raw()["entries"][SUBJECT]["resolved_by"], OTHER)


class ConsolidatedList(Base):
    def test_list_renders_every_open_claim_from_every_layer(self):
        self.claim(session="s-a", layer="fleet", asker=ASKER, text="pick a repair")
        self.claim(session="s-b", layer="worker", asker=OTHER, text="approve the merge")
        code, out, _ = self.listing()
        self.assertEqual(code, 0)
        self.assertIn("2 open claim(s)", out)
        self.assertIn("pick a repair", out)
        self.assertIn("approve the merge", out)

    def test_list_reports_no_claims_rather_than_printing_nothing(self):
        """A silent empty output is indistinguishable from the script failing."""
        code, out, _ = self.listing()
        self.assertEqual(code, 0)
        self.assertIn("no open claims", out)

    def test_a_resolved_subject_leaves_the_list(self):
        self.claim(asker=ASKER)
        self.resolve(asker=ASKER)
        code, out, _ = self.listing()
        self.assertEqual(code, 0)
        self.assertIn("no open claims", out)


class PruneNeverDeletesALiveClaim(Base):
    """The rule that keeps a shared ledger from re-introducing the per-layer bug.
    A shared file pruned by sweep-absence would let one layer delete another's
    marks; prune is by age and liveness instead."""

    def test_an_old_open_claim_on_a_live_session_survives(self):
        self.claim(asker=ASKER)
        self.backdate(SUBJECT, hours_ago=48)
        self.live(SUBJECT)
        code, out, _ = self._run(["prune", "--max-age-hours", "24"])
        self.assertEqual(code, 0)
        self.assertIn(SUBJECT, self.raw()["entries"])
        self.assertIn("kept 1", out)

    def test_an_old_open_claim_on_a_dead_session_is_dropped(self):
        self.claim(asker=ASKER)
        self.backdate(SUBJECT, hours_ago=48)
        code, out, _ = self._run(["prune", "--max-age-hours", "24"])
        self.assertEqual(code, 0)
        self.assertNotIn(SUBJECT, self.raw()["entries"])

    def test_an_old_resolved_claim_is_dropped_even_when_live(self):
        """A resolved mark on a still-live session is history, not a hold."""
        self.claim(asker=ASKER)
        self.resolve(asker=ASKER)
        self.backdate(SUBJECT, hours_ago=48, state="resolved")
        self.live(SUBJECT)
        self._run(["prune", "--max-age-hours", "24"])
        self.assertNotIn(SUBJECT, self.raw()["entries"])

    def test_a_recent_claim_survives_either_way(self):
        """Negative control for the age branch itself."""
        self.claim(asker=ASKER)
        self._run(["prune", "--max-age-hours", "24"])
        self.assertIn(SUBJECT, self.raw()["entries"])


class FailsLoudlyRatherThanSplittingTheLedger(Base):
    def test_no_asker_id_is_an_error_not_a_silent_write(self):
        """A wrong or missing asker id silently splits the ledger in two, and both
        halves look healthy -- so this must refuse rather than guess."""
        code, _, err = self._run(
            ["claim", "--session", SUBJECT, "--layer", "fleet"], env_asker=False
        )
        self.assertNotEqual(code, 0)
        self.assertIn("no asker session id", err)

    def test_a_corrupt_ledger_reads_empty_rather_than_raising(self):
        with open(al.LEDGER, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        code, out, _ = self.listing()
        self.assertEqual(code, 0)
        self.assertIn("no open claims", out)

    def test_a_claim_survives_a_corrupt_ledger(self):
        with open(al.LEDGER, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        code, out, _ = self.claim(asker=ASKER)
        self.assertEqual(code, 0)
        self.assertEqual(self.raw()["entries"][SUBJECT]["asker"], ASKER)


class LedgerShape(Base):
    def test_the_entry_is_keyed_on_the_subject_not_the_asker(self):
        """The join two layers can agree on is the blocked session, which both
        see. An asker-keyed ledger would let each layer hold its own copy."""
        self.claim(session=SUBJECT, layer="fleet", asker=ASKER)
        entry = self.raw()["entries"][SUBJECT]
        self.assertEqual(entry["session"], SUBJECT)
        self.assertEqual(entry["asker"], ASKER)
        self.assertEqual(entry["layer"], "fleet")
        self.assertEqual(entry["state"], "open")

    def test_whitespace_in_the_question_is_normalised(self):
        """A re-rendered line must not read as a different question."""
        self.claim(asker=ASKER, text="pick   a\n repair")
        self.assertEqual(self.raw()["entries"][SUBJECT]["text"], "pick a repair")

    def test_the_lock_is_a_sidecar_so_the_ledger_stays_valid_json(self):
        self.claim(asker=ASKER)
        self.assertTrue(os.path.exists(al.LOCK))
        self.assertIsInstance(self.raw(), dict)


class RowKeyedClaims(Base):
    """`manager-loop`'s `under-target` card claims ROWS, and its candidates are
    `phase: todo` rows that carry no blocked session -- so a session-only key left
    the branch's mandated disjointness check with no input it could be given.
    Measured on the branch's first live post (tick 33, 2026-10-02): the manager
    could not perform the claim for any of its three rows, read `list` by hand
    instead, and posted on that weaker basis. These tests pin the second subject
    and the one rule that differs for it."""

    def test_a_row_key_is_claimable_and_keyed_on_the_row(self):
        code, out, _ = self.claim_row()
        self.assertEqual(code, 0)
        self.assertIn("claimed", out)
        entry = self.raw()["entries"][ROW]
        self.assertEqual(entry["session"], ROW, "the row occupies the subject slot")
        self.assertEqual(entry["layer"], "worker")
        self.assertEqual(entry["state"], "open")

    def test_a_second_manager_is_refused_the_same_row(self):
        """The contention the branch actually produces: two topic managers, BOTH
        at --layer worker (the skill maps the goal/topic manager to `worker`),
        whose tracked sets each contain the same row. This is the check the branch
        could not perform at all before."""
        self.assertEqual(self.claim_row(layer="worker", asker=ASKER)[0], 0)
        code, out, _ = self.claim_row(layer="worker", asker=OTHER)
        self.assertEqual(code, al.HELD, "a second manager must not take the same row")
        self.assertIn("held by", out)
        self.assertIn(ASKER, out)

    def test_a_row_claim_resolves_by_row(self):
        """Without a row-keyed resolve the claim could never be released, and it
        would suppress the other layer until it aged out."""
        self.claim_row(asker=ASKER)
        code, out, _ = self.resolve_row(asker=ASKER)
        self.assertEqual(code, 0)
        self.assertIn("resolved", out)
        self.assertEqual(self.raw()["entries"][ROW]["state"], "resolved")

    def test_a_row_claim_prunes_by_age_alone(self):
        """A row key is not a session id and never appears in the registry, so the
        liveness guard cannot fire for it -- age alone bounds it. Deliberate: an
        invented liveness test for a row is worse than its honest absence."""
        self.claim_row(asker=ASKER)
        self.backdate(ROW, hours_ago=48)
        code, _, _ = self._run(["prune", "--max-age-hours", "24"])
        self.assertEqual(code, 0)
        self.assertNotIn(ROW, self.raw()["entries"])

    def test_a_row_claim_on_a_live_session_is_unaffected(self):
        """Negative control for the test above: a session claim still survives the
        same age once its session is live. The row path must not loosen this."""
        self.claim(asker=ASKER)
        self.backdate(SUBJECT, hours_ago=48)
        self.live(SUBJECT)
        self._run(["prune", "--max-age-hours", "24"])
        self.assertIn(SUBJECT, self.raw()["entries"])

    def test_a_row_key_is_normalised_like_the_question_text(self):
        """`--row` is free text, and an un-normalised key is interpolated raw into
        `list`'s one-line-per-entry render -- a newline in it splits the entry and
        misaligns every field after it. Normalising is symmetric, so it still
        round-trips between claim and resolve."""
        code, _, _ = self.claim_row(row="Some  Task\nRow")
        self.assertEqual(code, 0)
        self.assertIn("Some Task Row", self.raw()["entries"])

        code, out, _ = self.resolve_row(row="Some  Task\nRow")
        self.assertEqual(code, 0)
        self.assertIn("resolved", out)

    def test_an_empty_subject_key_is_refused(self):
        """An empty key can never be matched by a later claim or resolve. It is
        also the case the bare `or` broke: `--session ""` fell through to None,
        writing a `null` key that raises in json.dump(sort_keys=True) once the
        ledger holds any other entry."""
        code, _, err = self._run(
            ["claim", "--session", "", "--layer", "worker", "--asker", ASKER]
        )
        self.assertNotEqual(code, 0)
        self.assertIn("empty", err)

        code, _, err = self._run(
            ["claim", "--row", "", "--layer", "worker", "--asker", ASKER]
        )
        self.assertNotEqual(code, 0)
        self.assertIn("empty", err)

    def test_list_renders_a_row_keyed_entry(self):
        """The consolidated list is what makes one ask cover every subject; a
        row-keyed entry must render there, not vanish."""
        self.claim_row(asker=ASKER, text="pick a row")
        code, out, _ = self.listing()
        self.assertEqual(code, 0)
        self.assertIn("1 open claim(s)", out)
        self.assertIn(ROW, out)
        self.assertIn("pick a row", out)

    def test_a_row_claim_survives_a_prune_while_fresh(self):
        """The surviving direction, and the one the leak scenario depends on: a
        row claim's liveness guard can never fire, so age is the only thing
        bounding it -- which makes the fresh side worth pinning for rows, not just
        for sessions."""
        self.claim_row(asker=ASKER)
        code, out, _ = self._run(["prune", "--max-age-hours", "24"])
        self.assertEqual(code, 0)
        self.assertIn(ROW, self.raw()["entries"])

    def test_a_row_and_a_session_coexist_in_one_ledger(self):
        """Two subject kinds sharing `data['entries']` is exactly what this change
        makes newly possible, and it is the case `cmd_list` iterates over."""
        self.assertEqual(self.claim_row(row=ROW, asker=ASKER, text="a row")[0], 0)
        self.assertEqual(self.claim(session=SUBJECT, asker=OTHER, text="a session")[0], 0)
        code, out, _ = self.listing()
        self.assertEqual(code, 0)
        self.assertIn("2 open claim(s)", out)
        self.assertIn(ROW, out)
        self.assertIn(SUBJECT, out)
        self.assertIn("a row", out)
        self.assertIn("a session", out)

    def test_the_two_key_forms_address_the_same_slot(self):
        """`subject_of`'s stated contract -- to this ledger `--session` and `--row`
        are the same thing -- so a claim taken under one form is released under
        the other, in both directions."""
        self.claim_row(row=ROW, asker=ASKER)
        code, out, _ = self.resolve(session=ROW, asker=ASKER)
        self.assertEqual(code, 0)
        self.assertIn("resolved", out)
        self.assertEqual(self.raw()["entries"][ROW]["state"], "resolved")

        self.claim(session=SUBJECT, asker=ASKER)
        code, out, _ = self.resolve_row(row=SUBJECT, asker=ASKER)
        self.assertEqual(code, 0)
        self.assertIn("resolved", out)
        self.assertEqual(self.raw()["entries"][SUBJECT]["state"], "resolved")

    def test_a_row_claim_is_not_rescued_by_an_unrelated_live_session(self):
        """The design argument the row path rests on, pinned in the row direction:
        the guard matches the subject string against session ids, not "some session
        is live", so a live registry does not save a row claim.

        ⚠️ The bound is honest rather than structural: a registry entry whose
        `sessionId` IS the row key would rescue it. That is unreachable in
        practice -- one is a uuid, the other a task name -- and asserting the
        stronger claim here would pin a falsehood, so the real guard is pinned and
        the caveat stated."""
        self.claim_row(asker=ASKER)
        self.backdate(ROW, hours_ago=48)
        self.live("11111111-2222-3333-4444-555555555555")
        code, _, _ = self._run(["prune", "--max-age-hours", "24"])
        self.assertEqual(code, 0)
        self.assertNotIn(ROW, self.raw()["entries"])

    def test_resolve_row_falls_back_to_the_environment_asker(self):
        """`--asker` stays optional on the row path, exactly as on the session
        path: both reach the same cmd_resolve, which reads the env var."""
        self.claim_row(asker=ASKER)
        code, out, _ = self._run(["resolve", "--row", ROW])
        self.assertEqual(code, 0)
        self.assertIn("resolved", out)
        self.assertEqual(self.raw()["entries"][ROW]["state"], "resolved")

    def test_neither_key_is_a_usage_error(self):
        """Negative control: the key is required, so an omitted one must refuse
        rather than write an entry nothing can look up."""
        code, _, _ = self._run(["claim", "--layer", "worker", "--asker", ASKER])
        self.assertNotEqual(code, 0)

    def test_both_keys_is_a_usage_error(self):
        """Two subjects in one call has no meaning -- one slot, one key."""
        code, _, _ = self._run(
            [
                "claim",
                "--session",
                SUBJECT,
                "--row",
                ROW,
                "--layer",
                "worker",
                "--asker",
                ASKER,
            ]
        )
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
