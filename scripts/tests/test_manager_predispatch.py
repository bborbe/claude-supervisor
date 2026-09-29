#!/usr/bin/env python3
"""Tests for scripts/manager-predispatch.py.

The load-bearing properties, in the order the criteria grade them:

1. A no-change tree reports NOCHANGE and a change reports CHANGE — the saving itself.
2. **Liveness is in the digest**, so a worker dying moves it. Without this the gate
   would replay a stored table straight over a death.
3. **The negative control**: a LIVE worker whose heartbeat is older than the TTL must
   NOT read as dead. Property 2 alone is satisfied by a build that answers "dead" too
   eagerly and mis-reports live workers — positive without negative is a probe that
   cannot fail, so both are asserted here.
4. Fail-open: a missing, unreadable or table-less state file, and an unresolvable
   subject, all report CHANGE rather than "no change".
5. The stored table is written link-free and 0600 — the rendered frame's OSC 8 jump
   links carry the jump token, and this file lives on disk to be copied and pasted.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import io
import json
import os
import stat
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "manager-predispatch.py")

TASK = """---
status: in_progress
phase: execution
claude_session_id: {sid}
goals:
    - '[[AGoal]]'
---
Tags: [[Task]]

---

# Tasks

- [ ] one
- [x] two

# Progress

{progress}
"""

TOPIC = """---
page_type: topic
---
Tags: [[Topic]]

---

## Goals

- [[AGoal]]
- [[ATask]]
"""

GOAL = """---
page_type: goal
---
Tags: [[Goal]]

---

# Tasks

1. [[ATask]]
"""


def load(state_dir):
    os.environ["MANAGER_PREDISPATCH_STATE_DIR"] = state_dir
    spec = importlib.util.spec_from_file_location("manager_predispatch", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.vault = os.path.join(self.tmp, "vault")
        for d in ("25 Tasks", "23 Topics", "24 Goals"):
            os.makedirs(os.path.join(self.vault, d))
        self.m = load(os.path.join(self.tmp, "state"))
        self.m.REGISTRY_DIR = os.path.join(self.tmp, "sessions")
        self.m.FEED_DIR = os.path.join(self.tmp, "attention")
        self.m.HEARTBEAT_DIR = os.path.join(self.tmp, "live")
        for d in (self.m.REGISTRY_DIR, self.m.FEED_DIR, self.m.HEARTBEAT_DIR):
            os.makedirs(d)
        self.write("23 Topics/ATopic.md", TOPIC)
        self.write("24 Goals/AGoal.md", GOAL)
        self.task("ATask", sid="")
        self.task("AGoalTask", sid="", goals="AGoal")

    def write(self, rel, text):
        with open(os.path.join(self.vault, rel), "w", encoding="utf-8") as fh:
            fh.write(text)

    def task(self, name, sid="", progress="", goals=None):
        self.write(
            "25 Tasks/%s.md" % name,
            TASK.format(sid=sid, progress=progress).replace(
                "[[AGoal]]", "[[%s]]" % (goals or "AGoal")
            ),
        )

    def registry(self, sid, status="idle"):
        with open(os.path.join(self.m.REGISTRY_DIR, "1.json"), "w") as fh:
            json.dump({"sessionId": sid, "pid": os.getpid(), "status": status}, fh)

    def heartbeat(self, sid, age):
        p = os.path.join(self.m.HEARTBEAT_DIR, "%s.json" % sid)
        with open(p, "w") as fh:
            fh.write("{}")
        os.utime(p, (time.time() - age, time.time() - age))

    def run_gate(self, *argv, stdin=""):
        """-> (rc, stdout). stdin is fed only to --save."""
        import sys

        old_in, old_out = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = io.StringIO(stdin), io.StringIO()
        try:
            rc = self.m.main(list(argv) + ["--vault", self.vault])
            return rc, sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = old_in, old_out

    def save(self, subject, table="Subject: x\n+---+\n| a |\n+---+\n"):
        return self.run_gate("--subject", subject, "--save", stdin=table)

    def check(self, subject):
        return self.run_gate("--subject", subject, "--check")

    def prime(self, subject):
        self.save(subject)
        rc, out = self.check(subject)
        self.assertEqual(rc, self.m.EXIT_NOCHANGE, out)

    def read_state(self, subject):
        with open(self.m.state_path(subject), encoding="utf-8") as fh:
            return fh.read()


class TestVerdict(Base):
    def test_first_run_is_change_not_nochange(self):
        """Fail-open: a gate with no state cannot claim 'no change'."""
        rc, out = self.check("ATopic")
        self.assertEqual(rc, self.m.EXIT_CHANGE)
        self.assertIn("first run", out)

    def test_unchanged_tree_is_nochange(self):
        self.prime("ATopic")
        rc, _ = self.check("ATopic")
        self.assertEqual(rc, self.m.EXIT_NOCHANGE)

    def test_status_flip_is_change(self):
        self.prime("ATopic")
        self.task("ATask", sid="", progress="")  # rewrite with a different status
        self.write(
            "25 Tasks/ATask.md",
            TASK.format(sid="", progress="").replace("in_progress", "completed"),
        )
        rc, _ = self.check("ATopic")
        self.assertEqual(rc, self.m.EXIT_CHANGE)

    def test_progress_write_is_change(self):
        """The Progress hash is the signal that status/phase alone would miss."""
        self.prime("ATopic")
        self.task("ATask", sid="", progress="- 2026-09-26: moved.")
        rc, _ = self.check("ATopic")
        self.assertEqual(rc, self.m.EXIT_CHANGE)

    def test_print_replays_stored_table(self):
        self.save("ATopic", "Subject: x\n+---+\n| a |\n+---+\n")
        rc, out = self.run_gate("--subject", "ATopic", "--print")
        self.assertEqual(rc, self.m.EXIT_NOCHANGE)
        self.assertIn(self.m.NO_CHANGE_MARKER, out)
        self.assertIn("| a |", out)

    def test_goal_and_topic_both_resolve(self):
        for subject in ("ATopic", "AGoal"):
            rc, out = self.check(subject)
            self.assertEqual(rc, self.m.EXIT_CHANGE, subject)
            self.assertNotIn("unresolvable", out)

    def test_a_goal_page_without_page_type_still_resolves(self):
        """The goal branch keys on the folder, not on `page_type: goal`.

        Measured 2026-09-27 in the primary vault: 88 of 258 goal pages carried no `page_type: goal`
        and were therefore unresolvable to this gate *and* to the commands that defer to
        it, so a third of the goal branch was unreachable. The field bought no
        discrimination — the convention guides it was meant to reject live outside the
        folder, and an exact basename match inside it already excludes them.

        Negative control: `test_goal_and_topic_both_resolve` above pins the tagged case
        and `test_unknown_subject_fails_open` pins a genuinely absent page, so this cannot
        be satisfied by resolving everything.
        """
        self.write("24 Goals/UntaggedGoal.md", GOAL.replace("page_type: goal\n", ""))
        self.task("UntaggedTask", sid="", goals="UntaggedGoal")
        rc, out = self.check("UntaggedGoal")
        self.assertEqual(rc, self.m.EXIT_CHANGE, out)
        self.assertNotIn("unresolvable", out)

    def test_unknown_subject_fails_open(self):
        rc, out = self.check("No Such Subject")
        self.assertEqual(rc, self.m.EXIT_CHANGE)
        self.assertIn("fail-open", out)


class TestLiveness(Base):
    """Property 2 and its negative control — the pair SC3 is graded on."""

    def digest_with(self, sid):
        self.task("ATask", sid=sid)
        _, _, payload, _ = self.m.evaluate(self.vault, "ATopic")
        return self.m.digest_of(payload["tracked"])

    def test_dead_worker_moves_the_digest(self):
        """A worker dying is a change — it must never be swallowed."""
        live = self.digest_with("s-live")
        self.registry("s-live")
        live_now = self.digest_with("s-live")
        dead = self.digest_with("s-dead")
        self.assertNotEqual(live, dead, "an absent session did not move the digest")
        self.assertNotEqual(live, live_now, "gaining a live session did not move the digest")

    def test_live_worker_with_stale_heartbeat_is_not_dead(self):
        """The NEGATIVE CONTROL.

        A live worker whose heartbeat is older than the TTL, but whose session is
        still in the registry against a running pid, must read live. A build that
        decides 'dead' by heartbeat age alone passes the positive test above and
        fails this one.
        """
        self.heartbeat("s-live", age=self.m.HEARTBEAT_TTL_SECONDS * 100)
        self.registry("s-live")
        tracked = [{"name": "T", "session": "s-live"}]
        self.m.enrich_liveness(tracked, self.m.read_registry(), self.m.read_feed())
        self.assertEqual(tracked[0]["liveness"], self.m.LIVENESS_LIVE)

    def test_headless_worker_with_fresh_heartbeat_is_live(self):
        """The case the registry structurally cannot see."""
        self.heartbeat("s-headless", age=1)
        tracked = [{"name": "T", "session": "s-headless"}]
        self.m.enrich_liveness(tracked, self.m.read_registry(), self.m.read_feed())
        self.assertEqual(tracked[0]["liveness"], self.m.LIVENESS_LIVE)

    def test_stale_heartbeat_without_registry_is_none(self):
        """A killed server never clears its stamps, so age — not existence — decides."""
        self.heartbeat("s-gone", age=self.m.HEARTBEAT_TTL_SECONDS * 100)
        tracked = [{"name": "T", "session": "s-gone"}]
        self.m.enrich_liveness(tracked, self.m.read_registry(), self.m.read_feed())
        self.assertEqual(tracked[0]["liveness"], self.m.LIVENESS_NONE)

    def test_waiting_session_is_parked_not_live(self):
        """A newly-blocked worker is a change too."""
        self.registry("s-wait", status="waiting")
        tracked = [{"name": "T", "session": "s-wait"}]
        self.m.enrich_liveness(tracked, self.m.read_registry(), self.m.read_feed())
        self.assertEqual(tracked[0]["liveness"], self.m.LIVENESS_PARKED)


class TestFailOpen(Base):
    def test_corrupt_state_is_change(self):
        self.save("ATopic")
        with open(self.m.state_path("ATopic"), "w") as fh:
            fh.write("{not json")
        rc, out = self.check("ATopic")
        self.assertEqual(rc, self.m.EXIT_CHANGE)
        self.assertIn("parse error", out)

    def test_state_without_a_table_is_change(self):
        """A snapshot with no table cannot be replayed, so it cannot make a run free."""
        self.save("ATopic")
        data = json.loads(self.read_state("ATopic"))
        data["table"] = ""
        with open(self.m.state_path("ATopic"), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        rc, out = self.check("ATopic")
        self.assertEqual(rc, self.m.EXIT_CHANGE)
        self.assertIn("no stored table", out)

    def test_empty_table_is_refused_and_state_untouched(self):
        self.save("ATopic", "good table\n")
        before = self.read_state("ATopic")
        rc, _ = self.run_gate("--subject", "ATopic", "--save", stdin="   \n")
        self.assertEqual(rc, self.m.EXIT_CHANGE)
        self.assertEqual(self.read_state("ATopic"), before)

    def test_save_on_an_unresolvable_subject_is_change_not_a_crash(self):
        """`--save` owes the contract its sibling `--check` already honours: an
        unresolvable subject is a CHANGE verdict (exit 10), never a traceback.

        Nothing can be snapshotted for a subject with no page, so the save cannot
        happen — but the caller reads a crash as a failure rather than as a verdict to
        gate on, and a gate that cannot report "changed" re-sweeps the subject forever.

        Negative control: the guard must not be satisfied by refusing every save —
        `TestStorage.test_save_reports_change_on_success` asserts a resolvable subject
        still saves, and `test_empty_table_is_refused_and_state_untouched` above pins
        the other refusal that precedes this one.
        """
        rc, out = self.run_gate(
            "--subject", "No Such Subject", "--save", stdin="Subject: x\n+---+\n"
        )
        self.assertEqual(rc, self.m.EXIT_CHANGE, out)
        self.assertIn("CHANGE fail-open:", out)


class TestStorage(Base):
    def test_table_is_stored_link_free(self):
        """The OSC 8 URI carries the jump token; the on-disk copy must not."""
        link = "\x1b]8;;http://127.0.0.1:1337/jump?pane=9&t=SECRET\x07Session\x1b]8;;\x07"
        self.save("ATopic", "Subject: x\n%s\n" % link)
        stored = json.loads(self.read_state("ATopic"))["table"]
        self.assertNotIn("SECRET", stored)
        self.assertIn("Session", stored)

    def test_state_file_is_0600(self):
        self.save("ATopic")
        mode = stat.S_IMODE(os.stat(self.m.state_path("ATopic")).st_mode)
        self.assertEqual(mode, 0o600, "state file is %o, want 600" % mode)

    def test_save_reports_change_on_success(self):
        """Exit 10 from --save means 'this run saw a change', not a failure."""
        rc, out = self.save("ATopic")
        self.assertEqual(rc, self.m.EXIT_CHANGE)
        self.assertIn("SAVED", out)


class TestPayloadWriter(Base):
    """`--write-payload` is the writer every tick goes through — the sweep reader calls it
    as its own last step — and it is the one writer that must not put the jump token on
    disk. Graded separately from `--save` because the two are different sinks: the stored
    record was already link-free and 0600, while this file sat 0644 carrying the link,
    which is exactly why the defect outlived a fix to its sibling.
    """

    def payload(self, subject, table):
        return self.run_gate("--subject", subject, "--write-payload", stdin=table)

    def read_payload(self, subject):
        with open(self.m.payload_path(subject), encoding="utf-8") as fh:
            return fh.read()

    def test_payload_is_written_link_free(self):
        """The OSC 8 URI carries the jump token; the on-disk copy must not.

        The positive control is in the same test: the table handed in provably *did* carry
        a link, so this cannot pass on a frame that never produced one. The exposure is
        intermittent — measured 2026-09-29, 1 link at 11:28 and 0 at 11:56 on the same
        path — and the variable is the reader's rendering choice, not the writer's, so an
        absence only counts against a frame with something to strip.
        """
        link = "\x1b]8;;http://127.0.0.1:1337/jump?pane=9&t=SECRET\x07Session\x1b]8;;\x07"
        table = "Subject: x\n%s\n" % link
        self.assertIn("SECRET", table)
        rc, out = self.payload("ATopic", table)
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        written = self.read_payload("ATopic")
        self.assertNotIn("SECRET", written)
        self.assertNotIn("\x1b]8;", written)
        self.assertIn("Session", written)

    def test_payload_file_is_0600(self):
        """0600 at creation, not by a chmod after it: the file is never world-readable,
        not even for the instant between the write and the os.replace."""
        self.payload("ATopic", "Subject: x\n+---+\n")
        mode = stat.S_IMODE(os.stat(self.m.payload_path("ATopic")).st_mode)
        self.assertEqual(mode, 0o600, "payload file is %o, want 600" % mode)

    def test_rewrite_does_not_leave_a_world_readable_file_behind(self):
        """A file already sitting at 0644 must not survive the next write.

        `os.replace` swaps the directory entry, so the mode that lands is the one set at
        creation — but that is a property of the implementation rather than of the
        contract, and 0644-with-a-token is the exact state the measured defect left.
        """
        path = self.m.payload_path("ATopic")
        self.payload("ATopic", "Subject: x\n+---+\n")
        os.chmod(path, 0o644)
        self.payload("ATopic", "Subject: y\n+---+\n")
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o600, "payload file is %o, want 600" % mode)

    def test_payload_refuses_an_empty_table_and_leaves_the_previous_file(self):
        """Pinned alongside the new assertions so a later edit to this branch cannot
        trade the empty-stdin guard away for the strip."""
        self.payload("ATopic", "Subject: x\n+---+\n")
        rc, _ = self.payload("ATopic", "   \n")
        self.assertEqual(rc, self.m.EXIT_USAGE)
        self.assertEqual(self.read_payload("ATopic"), "Subject: x\n+---+\n")


class TestVaultRootBoundary(Base):
    """A bad `--vault` is a usage error, never a missing subject page.

    Given a vault NAME the gate joined it as a relative path, both `os.path.exists`
    calls were False, and `resolve_subject` blamed the SUBJECT — reporting a page that
    is present on disk as absent. Two things have to hold: the argument is named as the
    culprit, and the subject message stays reserved for a real root.
    """

    def run_gate_raw(self, *argv, stdin=""):
        """-> (rc, stdout+stderr) with no `--vault` appended by the caller."""
        import sys

        old_in, old_out, old_err = sys.stdin, sys.stdout, sys.stderr
        sys.stdin, sys.stdout, sys.stderr = (
            io.StringIO(stdin),
            io.StringIO(),
            io.StringIO(),
        )
        try:
            rc = self.m.main(list(argv))
            return rc, sys.stdout.getvalue() + sys.stderr.getvalue()
        finally:
            sys.stdin, sys.stdout, sys.stderr = old_in, old_out, old_err

    def test_vault_name_is_a_usage_error_not_a_missing_page(self):
        rc, out = self.run_gate_raw("--vault", "vault", "--subject", "ATopic", "--check")
        self.assertEqual(rc, self.m.EXIT_USAGE, out)
        self.assertIn("--vault", out)
        self.assertNotIn("no subject page", out)

    def test_vault_name_is_rejected_from_the_vaults_own_parent(self):
        """The regression: from the vault's PARENT a name resolved to a real vault and
        gated it silently, so the same call meant different trees in different cwds."""
        old = os.getcwd()
        os.chdir(os.path.dirname(self.vault))
        try:
            rc, out = self.run_gate_raw(
                "--vault",
                os.path.basename(self.vault),
                "--subject",
                "ATopic",
                "--check",
            )
        finally:
            os.chdir(old)
        self.assertEqual(rc, self.m.EXIT_USAGE, out)
        self.assertNotIn("no subject page", out)

    def test_non_vault_directory_is_a_usage_error(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        rc, out = self.run_gate_raw("--vault", plain, "--subject", "ATopic", "--check")
        self.assertEqual(rc, self.m.EXIT_USAGE, out)
        self.assertIn("not a vault root", out)
        self.assertNotIn("no subject page", out)

    def test_missing_absolute_path_is_a_usage_error(self):
        rc, out = self.run_gate_raw(
            "--vault", os.path.join(self.tmp, "nope"), "--subject", "ATopic", "--check"
        )
        self.assertEqual(rc, self.m.EXIT_USAGE, out)
        self.assertIn("not a directory", out)

    def test_absent_subject_under_a_real_root_still_fails_open(self):
        """Negative control: the boundary guard must not swallow the fail-open path a
        genuinely absent subject relies on, or the fix would trade one blind gate for
        another."""
        rc, out = self.run_gate_raw(
            "--vault", self.vault, "--subject", "No Such Subject", "--check"
        )
        self.assertEqual(rc, self.m.EXIT_CHANGE, out)
        self.assertIn("no subject page", out)


class TestTrackedArtifacts(Base):
    """The three modes the dispatch path depends on: the tracked set travels as a file,
    the bucket half is persisted, and a set that disagrees with the gate's own membership
    is reported rather than accepted."""

    def setUp(self):
        super().setUp()
        # `--compare-tracked` derives the loop snapshot path from SWEEP_GATE_BASE. Point it
        # at a per-test dir so a real ~/.claude/state/sweep-gate-loop is never read — a test
        # that reads live state passes or fails on the fleet's mood, not on the code.
        os.environ["SWEEP_GATE_BASE"] = os.path.join(self.tmp, "loop")

    def tracked(self, subject, names):
        return self.run_gate(
            "--subject", subject, "--write-tracked", stdin="\n".join(names) + "\n"
        )

    def buckets(self, subject, payload):
        return self.run_gate(
            "--subject", subject, "--write-buckets", stdin=json.dumps(payload)
        )

    def snapshot(self, subject, names):
        d = os.path.join(os.environ["SWEEP_GATE_BASE"], os.path.basename(self.vault).lower())
        os.makedirs(d, exist_ok=True)
        with open(
            os.path.join(d, "%s.snapshot.json" % self.m.slug(subject)), "w", encoding="utf-8"
        ) as fh:
            json.dump({"tasks": {n: {} for n in names}}, fh)

    def test_write_tracked_prints_the_path_it_wrote(self):
        rc, out = self.tracked("ATopic", ["ATask", "AGoalTask"])
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        path = out.strip()
        self.assertEqual(path, self.m.tracked_path("ATopic"))
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "ATask\nAGoalTask\n")

    def test_write_tracked_refuses_an_empty_set_and_leaves_the_previous_file(self):
        self.tracked("ATopic", ["ATask"])
        rc, _ = self.tracked("ATopic", [])
        self.assertEqual(rc, self.m.EXIT_USAGE)
        with open(self.m.tracked_path("ATopic"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "ATask\n")

    def test_write_buckets_refuses_a_count_where_names_are_required(self):
        """The shape check that earns its keep: a bucket mapped to a count *looks* like a
        classification and gates nothing."""
        rc, _ = self.buckets("ATopic", {"done": 5})
        self.assertEqual(rc, self.m.EXIT_USAGE)

    def test_write_buckets_refuses_an_empty_object(self):
        rc, _ = self.buckets("ATopic", {})
        self.assertEqual(rc, self.m.EXIT_USAGE)

    def test_write_buckets_refuses_a_bucket_mapped_to_an_empty_list(self):
        """The vacuous-truth hole. `all(...)` over `[]` is True, so `{"done": []}` passed
        the very check whose message says "must map to a non-empty list of names" — and an
        all-empty set is precisely the shape that cannot gate, so it staged at exit 0 and
        `--save` wrote it, holding the drive leg's whole ready batch at clause (0)."""
        rc, _ = self.buckets("ATopic", {"done": []})
        self.assertEqual(rc, self.m.EXIT_USAGE)

    def test_write_buckets_refuses_one_empty_bucket_among_full_ones(self):
        """The mixed case: a single empty bucket is enough to poison the half, and it is
        the shape a column-0 mis-parse produces — every declared bucket present, each one
        empty, so the set *looks* structurally valid."""
        rc, _ = self.buckets("ATopic", {"done": ["ATask"], "problem": []})
        self.assertEqual(rc, self.m.EXIT_USAGE)

    def test_a_corrected_restage_lands_on_an_unchanged_tree(self):
        """The no-heal, and the negative control for it. `digest_of` covers the tracked set
        only, so a corrected set staged against an unchanged tree used to return
        `SAVED no-change (digest equal)` at exit 0 and never reach the store — leaving the
        bad record in place until the tree next moved. The bucket half is now a save input
        in its own right, so the second save below writes even though the digest is equal.
        """
        rc, out = self.buckets("ATopic", {"done": ["ATask"]})
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        rc, out = self.run_gate(
            "--subject", "ATopic", "--save", "--buckets", out.strip(), stdin="t\n"
        )
        self.assertEqual(rc, self.m.EXIT_CHANGE, out)
        self.assertEqual(
            json.loads(self.read_state("ATopic"))["bucket_sets"], {"done": ["ATask"]}
        )

        # Same tree, corrected classification: the digest is equal, the half moved.
        rc, out = self.buckets("ATopic", {"done": ["ATask"], "problem": ["AGoalTask"]})
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        rc, out = self.run_gate(
            "--subject", "ATopic", "--save", "--buckets", out.strip(), stdin="t\n"
        )
        self.assertEqual(rc, self.m.EXIT_CHANGE, out)
        self.assertEqual(
            json.loads(self.read_state("ATopic"))["bucket_sets"],
            {"done": ["ATask"], "problem": ["AGoalTask"]},
        )

    def test_a_save_with_no_buckets_on_an_unchanged_tree_still_writes_nothing(self):
        """The other side of the same decision, so the new clause cannot pass by writing
        unconditionally: with no `--buckets` and an equal digest, the record is untouched —
        the documented no-change contract, which is correct and must survive."""
        self.save("ATopic")
        before = self.read_state("ATopic")
        rc, out = self.run_gate("--subject", "ATopic", "--save", stdin="t\n")
        self.assertEqual(rc, self.m.EXIT_NOCHANGE, out)
        self.assertEqual(self.read_state("ATopic"), before)

    def test_save_records_the_bucket_sets_under_the_key(self):
        rc, out = self.buckets(
            "ATopic", {"done": ["ATask"], "ready-to-start": ["AGoalTask"]}
        )
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        rc, out = self.run_gate(
            "--subject", "ATopic", "--save", "--buckets", out.strip(), stdin="t\n"
        )
        self.assertEqual(rc, self.m.EXIT_CHANGE, out)
        self.assertEqual(
            json.loads(self.read_state("ATopic"))["bucket_sets"],
            {"done": ["ATask"], "ready-to-start": ["AGoalTask"]},
        )

    def test_save_without_buckets_leaves_the_key_absent(self):
        """Absent, not empty: a record that lacks the half must read as "not persisted"
        rather than as "persisted and empty"."""
        self.save("ATopic")
        self.assertNotIn("bucket_sets", json.loads(self.read_state("ATopic")))

    def test_save_with_an_unreadable_buckets_path_is_a_usage_error(self):
        """Deliberately NOT a fail-open. The gate's fail-open rule exists so an unreadable
        *state* file re-sweeps; here the caller believes it supplied the half, and a record
        written without it would read back as persisted when it is not."""
        rc, out = self.run_gate(
            "--subject",
            "ATopic",
            "--save",
            "--buckets",
            os.path.join(self.tmp, "nope.json"),
            stdin="t\n",
        )
        self.assertEqual(rc, self.m.EXIT_USAGE, out)

    def test_compare_reports_identical_memberships_with_both_counts(self):
        self.tracked("ATopic", ["ATask", "AGoalTask"])
        self.snapshot("ATopic", ["ATask", "AGoalTask"])
        rc, out = self.run_gate("--subject", "ATopic", "--compare-tracked")
        self.assertEqual(rc, self.m.EXIT_NOCHANGE, out)
        self.assertIn("caller 2 names", out)
        self.assertIn("gate 2 names", out)
        self.assertIn("identical", out)

    def test_compare_reports_a_divergence_in_both_directions(self):
        self.tracked("ATopic", ["ATask", "OnlyMine"])
        self.snapshot("ATopic", ["ATask", "OnlyGate"])
        rc, out = self.run_gate("--subject", "ATopic", "--compare-tracked")
        self.assertEqual(rc, self.m.EXIT_DIVERGENT, out)
        self.assertIn("DIVERGENCE", out)
        self.assertIn("caller-only: OnlyMine", out)
        self.assertIn("gate-only: OnlyGate", out)

    def test_compare_never_reports_agreement_when_it_could_not_read(self):
        """An absent snapshot shares exit 10 with a divergence and must never exit 0:
        "I could not check" and "they match" are precisely the two states this mode
        exists to tell apart."""
        self.tracked("ATopic", ["ATask"])
        rc, out = self.run_gate("--subject", "ATopic", "--compare-tracked")
        self.assertEqual(rc, self.m.EXIT_DIVERGENT, out)
        self.assertIn("unavailable", out)
        self.assertIn("NOT compared", out)

    def test_compare_without_a_staged_set_is_a_usage_error(self):
        rc, _ = self.run_gate("--subject", "ATopic", "--compare-tracked")
        self.assertEqual(rc, self.m.EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
