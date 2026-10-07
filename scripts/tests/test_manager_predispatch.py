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

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone

# Side effect only: points the start-time cache at an isolated per-run store, in one shared
# home so five suites cannot each assign the same key and leave only the last standing.
import start_cache_isolation  # noqa: E402,F401

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "manager-predispatch.py")

# The fixture vault's bucket declaration, shaped like the real runbook's § Step 4: a
# parenthesised `/`-separated run on the marker line, then a dispositions clause. The
# prose note deliberately names two SUPERSEDED spellings (`problem`, `orphan`) that appear
# nowhere in the parenthesised run — a parser that collects words off the line admits them,
# and a clean fixture with no removal note passes against that broken parser. That is the
# regression case, not the happy path.
#
# ⚠️ The run carries the live runbook's own MARKDOWN EMPHASIS (`**ready-to-start**` …), and
# that is load-bearing rather than cosmetic: the live `65 Runbooks/Manager Session.md`
# renders four of its bucket names bold, so a fixture that declares them plain cannot
# reproduce the defect at all. Measured 2026-10-03 — the parser kept the markers, its
# declared set carried `**ready-to-start**` while `ACTIONABLE_BUCKETS` held the plain name,
# and no key spelling satisfied both halves. A plain fixture passes against that broken
# parser, which is exactly how the regression shipped.
RUNBOOK = """---
page_type: runbook
---

# Manager Session

**Step 4 — Classify into the full bucket set** — **this is the canonical set; there is no second one.** (progressing / stuck / waiting-on-human / waiting-approval / parked-on-unregistered-gate / done / **ready-to-start** / **blocked-upstream** / **close-me** / **orphaned**):

⚠️ The `problem` cell was renamed `stuck` on 2026-09-20, and `orphan` was renamed `orphaned`; both old spellings survive in this file's history and must not be read back as members.

- **Non-bucket dispositions — `hold`, `backlog` and `👤 YOURS`.** These are **not** among the ten and are **not** escape hatches into one of them. A `hold` task renders `⏸️ blocked/hold`.
"""

# The 13 names the RUNBOOK above declares. Under direction (2) this tuple carries BOTH
# halves: it is the vocabulary a key must belong to, and — because the rule text's
# "every declared bucket must appear" is now enforced rather than merely stated — the set
# of keys a writable classification must carry in full.
DECLARED_NAMES = (
    "progressing",
    "stuck",
    "waiting-on-human",
    "waiting-approval",
    "parked-on-unregistered-gate",
    "done",
    "ready-to-start",
    "blocked-upstream",
    "close-me",
    "orphaned",
    "hold",
    "backlog",
    "👤 YOURS",
)


def full_set(**overrides):
    """Every declared name present, each mapped to `[]` unless overridden.

    The shape direction (2) makes the only writable one: a bucket mapped to `[]` is a
    declaration of absence ("evaluated, nothing in it"), and a bucket *missing* is an
    under-declaration the writer now refuses.

    ⚠️ The default is the quiet board, so a caller MUST populate at least one bucket —
    `full_set()` alone is all-empty, which the writer still refuses as the mis-parse
    signature (a non-empty tracked set always populates at least one bucket).
    """
    payload = {name: [] for name in DECLARED_NAMES}
    payload.update(overrides)
    return payload


# The two declaration lines' markers, matched exactly as `manager-predispatch.py` matches
# them. Restated here rather than imported from the script, so a test can pin WHICH line a
# mutation landed on — a mutation that drifted into prose would otherwise pass a
# line-agnostic assertion while proving nothing about the declaration the parser reads.
BUCKET_MARKER = "Step 4 — Classify into the full bucket set"
DISPOSITION_MARKER = "Non-bucket dispositions"


def live_proc_start(pid):
    """`pid`'s real start time as the registry writes `procStart` — ctime, UTC.

    A planted record needs this to be realistic. `session-liveness.py` now proves the pid
    belongs to the recorded session by comparing `procStart` against the live holder's
    `ps -o lstart=`, so a fixture that omits the field is UNKNOWN by design — the
    missing-field path, not the live one these tests mean to exercise.
    """
    out = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    if not out:
        return None
    return (
        datetime.strptime(out, "%a %b %d %H:%M:%S %Y")
        .astimezone(timezone.utc)
        .strftime("%a %b %d %H:%M:%S %Y")
    )

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

TASK_WITH_METRICS = """---
status: in_progress
phase: execution
goals:
    - '[[AGoal]]'
metrics_sessions:
    - session_id: {ids}
---
Tags: [[Task]]

---

# Tasks

- [ ] one

# Progress

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
        for d in ("25 Tasks", "23 Topics", "24 Goals", "65 Runbooks"):
            os.makedirs(os.path.join(self.vault, d))
        # The bucket vocabulary is read from the runbook, so every bucket-door test needs
        # one. Fail-closed means its absence is itself a usage error.
        self.write("65 Runbooks/Manager Session.md", RUNBOOK)
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
            json.dump(
                {
                    "sessionId": sid,
                    "pid": os.getpid(),
                    "procStart": live_proc_start(os.getpid()),
                    "status": status,
                },
                fh,
            )

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
        """Render the payload, then save with no stdin — the supported path.

        `--save` refuses a table on stdin, so the table reaches the store only through
        the payload the render wrote; that is what lets the record be dated from the
        payload's mtime instead of from save time.
        """
        return self.save_with_table(subject, table)

    def save_with_table(self, subject, table, *extra):
        """`--write-payload` then `--save`, so the record is dated from the payload."""
        rc, out = self.run_gate("--subject", subject, "--write-payload", stdin=table)
        if rc != self.m.EXIT_WRITE_OK:
            return rc, out
        return self.run_gate("--subject", subject, "--save", *extra)

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


class TestProgressHashContract(Base):
    """Pins `progress_hash`'s derivation, which its docstring now publishes.

    A reader outside this module is expected to reimplement the derivation from that
    prose, so each property is pinned against a **fixed byte string** — a hand-written
    literal such as `b"foo\\n"` — rather than by re-deriving the value the way the
    implementation does; a test that recomputes with the same expression drifts with the
    code and would have passed against both revisions of the docstring. ⚠️ **What is
    pinned is the byte string, not the hex.** `_sha()` still calls `hashlib.sha256` at run
    time, so a change to the *hashing algorithm* would move both sides together and pass;
    what fails here is a change to `_PROGRESS` or to the `[:16]` truncation, because group
    1 would stop matching the asserted bytes.
    """

    @staticmethod
    def _sha(raw):
        return hashlib.sha256(raw).hexdigest()[:16]

    def test_heading_line_is_excluded(self):
        self.assertEqual(
            self.m.progress_hash("# Progress\nfoo\n"), self._sha(b"foo\n")
        )

    def test_delimiter_newline_is_not_part_of_the_body(self):
        """The boundary is one byte off the obvious reading, in both directions."""
        # A blank line before the next heading is dropped; the delimiter `\n` is excluded.
        self.assertEqual(
            self.m.progress_hash("# Progress\nfoo\n\n# Other\nbar\n"),
            self._sha(b"foo\n"),
        )
        # No blank line: the content line's own trailing newline goes with the delimiter.
        self.assertEqual(
            self.m.progress_hash("# Progress\nfoo\n# Other\nbar\n"),
            self._sha(b"foo"),
        )

    def test_absent_section_hashes_the_empty_string(self):
        self.assertEqual(
            self.m.progress_hash("# Impact\nhello\n"), self._sha(b"")
        )

    def test_empty_section_at_eof_hashes_the_empty_string(self):
        self.assertEqual(self.m.progress_hash("# Progress\n"), self._sha(b""))

    def test_empty_section_followed_by_a_heading_does_NOT_hash_the_empty_string(self):
        """The surprising half, and the one the docstring spells out.

        `\\s*` backtracks to leave one newline for the literal `\\n`, so group 1 starts
        at the *next* heading and hashes the following section's body.
        """
        self.assertEqual(
            self.m.progress_hash("# Progress\n\n# Other\nbar\n"),
            self._sha(b"# Other\nbar\n"),
        )
        self.assertNotEqual(
            self.m.progress_hash("# Progress\n\n# Other\nbar\n"), self._sha(b"")
        )

    def test_truncation_is_the_first_sixteen_hex(self):
        h = self.m.progress_hash("# Progress\nfoo\n")
        self.assertEqual(len(h), 16)
        self.assertEqual(h, self._sha(b"foo\n"))


class TestActionablePassThrough(Base):
    """SC1's both directions: a READY-TO-START row in the stored classification forces the
    sweep, and a board without one keeps the saving.

    The defect these pin is a STEADY state, not a moved one. A ready-to-start row hashes
    the same on every tick — status, phase, Progress, session, liveness and stuck are all
    unchanged while it sits approved and unstarted — so before this clause the gate
    returned NOCHANGE forever and the act leg never ran. Measured 2026-10-01 on `Managers
    Spawn Interactive Claude Workers in the Cluster`: NO-CHANGE since 18:04 with 2
    ready-to-start and 1 waiting-approval row on the board, 0 agents dispatched.

    ⚠️ `waiting-approval` is deliberately NOT actionable, and the 2026-10-01 measurement
    above is why the distinction is easy to miss: it was counted then. It is a steady state
    the act leg cannot end — the row sits at `phase: todo`, and only the operator's
    `vault-cli task approve` moves it. Counting it made the no-change verdict unreachable
    for every topic carrying an approval queue, which is a managed topic's normal state.
    Measured 2026-10-03 on `Manager Layer`: 23 rows, `--check` answering CHANGE with the
    `actionable` reason on a tree whose digest had not moved.

    The no-actionable-row case is the negative control and it is load-bearing: the
    pass-through direction alone is satisfied by a gate hard-wired to return CHANGE, which
    would destroy the saving this whole file exists for.
    """

    def buckets(self, subject, payload):
        return self.run_gate(
            "--subject", subject, "--write-buckets", stdin=json.dumps(payload)
        )

    def classified(self, subject, payload):
        """Store a classification over an otherwise unchanged tree.

        `payload` is merged OVER `full_set()`, so a test names only the buckets it is about
        and every other declared name is carried empty. Under direction (2) a bucket
        *missing* is refused, so a sparse literal is no longer a writable set."""
        payload = {**full_set(), **payload}
        self.save(subject)
        rc, out = self.buckets(subject, payload)
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        rc, out = self.save_with_table(subject, "t\n", "--buckets", out.strip())
        self.assertEqual(rc, self.m.EXIT_CHANGE, out)

    def test_a_ready_to_start_row_passes_through_on_an_unchanged_tree(self):
        self.classified("ATopic", {"done": ["ATask"], "ready-to-start": ["AGoalTask"]})
        rc, out = self.check("ATopic")
        self.assertEqual(rc, self.m.EXIT_CHANGE, out)
        self.assertIn("actionable", out)
        self.assertIn("AGoalTask", out)

    def test_a_waiting_approval_row_keeps_the_saving_on_an_unchanged_tree(self):
        """The row waits on the OPERATOR, not on the loop: it leaves `waiting-approval` only
        through `vault-cli task approve`, which the act leg may not run. Sweeping cannot move
        it, so it must not suspend the saving."""
        self.classified("ATopic", {"done": ["ATask"], "waiting-approval": ["AGoalTask"]})
        rc, out = self.check("ATopic")
        self.assertEqual(rc, self.m.EXIT_NOCHANGE, out)
        self.assertNotIn("actionable", out)

    def test_a_classification_with_no_actionable_row_still_reports_nochange(self):
        """The negative control: `changed = True` unconditionally would pass every test
        above while disabling the gate."""
        self.classified("ATopic", {"done": ["ATask"], "progressing": ["AGoalTask"]})
        rc, out = self.check("ATopic")
        self.assertEqual(rc, self.m.EXIT_NOCHANGE, out)

    def test_an_absent_classification_still_reports_nochange(self):
        """The pre-existing contract, and why the clause reads the half defensively: a
        record with no `bucket_sets` behaves exactly as it did before this change."""
        self.prime("ATopic")
        rc, out = self.check("ATopic")
        self.assertEqual(rc, self.m.EXIT_NOCHANGE, out)

    def test_a_malformed_classification_changes_no_verdict(self):
        """A half that is not a dict of lists reads as "no actionable rows" — never an
        error, never a fail-open. The safe direction is a replay: the gate cannot invent a
        row it was not told about, and a wrong dispatch is the expensive failure."""
        self.save("ATopic")
        path = self.m.state_path("ATopic")
        record = json.loads(self.read_state("ATopic"))
        for bad in ("ready-to-start", ["ready-to-start"], {"ready-to-start": "ATask"}):
            record["bucket_sets"] = bad
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(record, fh)
            rc, out = self.check("ATopic")
            self.assertEqual(rc, self.m.EXIT_NOCHANGE, "%r -> %s" % (bad, out))

    def test_the_saving_returns_once_the_row_leaves_the_bucket(self):
        """Not a latch: a row the act leg actually opened reclassifies, and the next tick
        is free again — which is what keeps this from being a permanent cost."""
        self.classified("ATopic", {"ready-to-start": ["AGoalTask"]})
        rc, out = self.check("ATopic")
        self.assertEqual(rc, self.m.EXIT_CHANGE, out)
        rc, out = self.buckets(
            "ATopic", {**full_set(), "progressing": ["AGoalTask"]}
        )
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        rc, out = self.save_with_table("ATopic", "t\n", "--buckets", out.strip())
        self.assertEqual(rc, self.m.EXIT_CHANGE, out)
        rc, out = self.check("ATopic")
        self.assertEqual(rc, self.m.EXIT_NOCHANGE, out)

    def test_actionable_names_reads_only_the_ready_to_start_bucket(self):
        self.assertEqual(self.m.actionable_names(None), [])
        self.assertEqual(self.m.actionable_names({"done": ["A"]}), [])
        self.assertEqual(
            self.m.actionable_names(
                {"ready-to-start": ["B", "A"], "waiting-approval": ["C"], "done": ["D"]}
            ),
            ["A", "B"],
        )


class TestLiveness(Base):
    """Property 2 and its negative control — the pair SC3 is graded on."""

    def digest_with(self, sid):
        self.task("ATask", sid=sid)
        _, _, payload, _ = self.m.evaluate(self.vault, "ATopic")
        return self.m.digest_of(payload["tracked"])

    def digest_with_metrics_only(self, sid):
        """`digest_with`'s shape for a row whose ids live ONLY in `metrics_sessions`.

        `read_task` sets `session` from the frontmatter `claude_session_id` ALONE while
        `sessions` carries the whole id set, so this row reaches the digest with
        `session == ""` — the shape the unguarded term exists for. Driving it through
        `evaluate` (rather than asserting on `liveness_change_term` directly) is what
        makes the regression this guards against reproducible end to end.
        """
        self.write("25 Tasks/ATask.md", TASK_WITH_METRICS.format(ids=sid))
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

    def test_liveness_churn_alone_does_not_move_the_digest(self):
        """Task-SC2(a) — a worker opening or closing a gate is CHURN, not a change.

        The raw liveness word sat in the digest before this fix, so a `LIVE` -> `PARKED`
        flip — a worker starting or ending a turn — answered CHANGE on a tick where no
        row's eligibility moved. Measured 2026-09-30: three consecutive act legs returned
        the identical decision set on exactly such ticks. The digest must be identical
        across the flip.
        """
        self.registry("s-churn")
        live = self.digest_with("s-churn")
        self.registry("s-churn", status="waiting")
        parked = self.digest_with("s-churn")
        self.assertEqual(live, parked, "liveness churn alone moved the digest")

    def test_a_parked_flip_does_not_replay_a_stale_table(self):
        """The Status cell moves on `live` <-> `parked`, so exit 0 must not be claimed.

        The churn test above is why this one is needed, not a contradiction of it: the
        digest is deliberately death-only, so a worker opening a gate moves NO digest
        input — and the exit-0 branch then replays the stored table verbatim. One cell in
        that table is no longer true: the Status cell, which reads `⌛ waiting-on-human`
        for a parked row and `🔄 progressing` for a live one
        (`agents/manager-sweep-reader.md:136`). The stale cell is the *Status* cell and
        NOT the Session cell, which is `[<sid8>]` for both — `parked` is ALIVE, so it
        takes the live-shaped delimiter (step 7).

        So the digest stays pure and a SEPARATE clause carries this, exactly as
        `actionable_names` does: a replay is only free when the table it would print is
        still true.
        """
        self.registry("s-park", status="idle")
        self.task("ATask", sid="s-park")
        self.prime("ATopic")

        self.registry("s-park", status="waiting")  # same row, same sid, now parked
        rc, out = self.run_gate("--subject", "ATopic", "--print")
        self.assertEqual(
            rc,
            self.m.EXIT_CHANGE,
            "a parked flip replayed a stale table instead of forcing a re-render:\n" + out,
        )

    def test_a_still_live_owner_still_replays(self):
        """The negative control: the clause must not make the replay unreachable.

        Without this, a build that forces CHANGE on every liveness word — or on every
        run at all — satisfies the test above while destroying the saving the whole gate
        exists for.
        """
        self.registry("s-quiet", status="idle")
        self.task("ATask", sid="s-quiet")
        self.prime("ATopic")

        rc, out = self.run_gate("--subject", "ATopic", "--print")
        self.assertEqual(
            rc,
            self.m.EXIT_NOCHANGE,
            "an unchanged tree stopped replaying:\n" + out,
        )

    def test_the_parked_clause_does_not_move_the_digest(self):
        """`digest_of`'s contract survives: the reason is a SEPARATE clause.

        This is what separates the fix from reverting PR #154. Hashing the raw liveness
        word again would also stop the stale replay, and would put session churn back
        into the digest — the ~150k-token act legs #154 removed. The digest must be
        byte-identical across the flip while the gate still refuses to replay.
        """
        self.registry("s-sep", status="idle")
        self.task("ATask", sid="s-sep")
        self.prime("ATopic")
        live = json.loads(self.read_state("ATopic"))["digest"]

        self.registry("s-sep", status="waiting")
        _, _, payload, _ = self.m.evaluate(self.vault, "ATopic")
        self.assertEqual(
            self.m.digest_of(payload["tracked"]),
            live,
            "the parked clause moved the digest instead of riding beside it",
        )

    def _registry_file(self, fname, sid, status):
        """A second registry record. `Base.registry` always writes `1.json`, so a test
        needing TWO live sessions has no helper — this is that, kept local to the two
        multi-session tests rather than widening the shared fixture."""
        with open(os.path.join(self.m.REGISTRY_DIR, fname), "w") as fh:
            json.dump(
                {
                    "sessionId": sid,
                    "pid": os.getpid(),
                    "procStart": live_proc_start(os.getpid()),
                    "status": status,
                },
                fh,
            )

    def test_a_record_predating_the_parked_set_is_not_replayed(self):
        """The branch EVERY existing vault hits once, on its first run after upgrade.

        `save_stored` only began writing `parked` with this change, so every record already
        on disk carries no key. `parked_replay_reason` reads that as "cannot say" — a
        CHANGE — because a gate that reports "no change" when it cannot tell is the one
        failure worth spending a dispatch to avoid. Untested, a refactor flipping that
        branch to `None` would pass the whole suite and serve stale replays to every vault
        on upgrade.
        """
        self.registry("s-upgrade", status="idle")
        self.task("ATask", sid="s-upgrade")
        self.prime("ATopic")

        state = json.loads(self.read_state("ATopic"))
        del state["parked"]  # a record written before the field existed
        with open(self.m.state_path("ATopic"), "w", encoding="utf-8") as fh:
            json.dump(state, fh)

        rc, out = self.run_gate("--subject", "ATopic", "--print")
        self.assertEqual(rc, self.m.EXIT_CHANGE, out)
        self.assertIn("predates the parked set", out)

    def test_a_malformed_parked_set_is_not_replayed_as_an_upgrade(self):
        """A `parked` value that is not a list is malformed, not pre-field.

        Both refuse the replay, so the verdict is identical — but the reason line is what a
        reader acts on, and "this record predates the field" sends them looking for an
        upgrade that already happened. Pinned because the two cases share a predicate shape
        and only the message separates them.
        """
        self.registry("s-malformed", status="idle")
        self.task("ATask", sid="s-malformed")
        self.prime("ATopic")

        state = json.loads(self.read_state("ATopic"))
        state["parked"] = "ATask"  # a bare string where a list belongs
        with open(self.m.state_path("ATopic"), "w", encoding="utf-8") as fh:
            json.dump(state, fh)

        rc, out = self.run_gate("--subject", "ATopic", "--print")
        self.assertEqual(rc, self.m.EXIT_CHANGE, out)
        self.assertIn("malformed", out)

    def test_the_parked_set_compares_across_rows_not_just_one(self):
        """Two rows, one parking — the set comparison, not the degenerate one-row case.

        With a single tracked row the comparison is `[] != [name]` on either side, so a
        build that compared only the first row, or that read the set as a scalar, passes
        every other test in this class.
        """
        self.task("AGoalTask", sid="s-two-a")
        self.task("ATask", sid="s-two-b")
        self._registry_file("1.json", "s-two-a", "idle")
        self._registry_file("2.json", "s-two-b", "idle")
        # Both rows must actually be tracked, or the test silently degenerates into the
        # one-element case it exists to get away from.
        _, _, payload, _ = self.m.evaluate(self.vault, "ATopic")
        self.assertEqual(
            len(payload["tracked"]), 2, "fixture must track both rows for this to be a set"
        )
        self.prime("ATopic")

        # Park ONE of the two; the other stays live, so the sets differ by one name.
        self._registry_file("2.json", "s-two-b", "waiting")
        rc, out = self.run_gate("--subject", "ATopic", "--print")
        self.assertEqual(
            rc,
            self.m.EXIT_CHANGE,
            "one row parking was not seen across a two-row set:\n" + out,
        )

    def test_a_worker_dying_still_moves_the_digest(self):
        """Task-SC2(b) — the guard against the cheap fix.

        Dropping the liveness term outright satisfies the churn test above and regresses
        the graded death-detection (`liveness_of` -> `LIVENESS_NONE`). The SAME row on the
        SAME sid, its owner going `LIVE` -> `ABSENT`, must still move the digest.
        """
        self.registry("s-dies")
        live = self.digest_with("s-dies")
        os.remove(os.path.join(self.m.REGISTRY_DIR, "1.json"))
        dead = self.digest_with("s-dies")
        self.assertNotEqual(live, dead, "a worker dying did not move the digest")

    def test_death_is_detected_for_every_ownership_shape(self):
        """The term is UNGUARDED on purpose — a guard on any subset of the ownership
        signals drops death detection for the shapes it excludes.

        `read_task` sets `session` from the frontmatter `claude_session_id` alone while
        `sessions` carries the whole id set, and `liveness_of` probes `sessions` FIRST.
        A row whose ids live only in `metrics_sessions` is therefore owned, and its
        owner's death must move the digest exactly as a frontmatter-owned row's does.
        An earlier cut keyed the term on `session` alone and silently missed that shape.
        """
        term = self.m.liveness_change_term
        dead = self.m.LIVENESS_NONE

        # Death, across every shape ownership is expressed in.
        self.assertEqual(
            term({"name": "T", "session": "s-1", "liveness": dead}),
            1,
            "a frontmatter-owned row's death was not detected",
        )
        self.assertEqual(
            term({"name": "T", "session": "", "sessions": ["s-1"], "liveness": dead}),
            1,
            "an id-set-only row's death was not detected",
        )
        # A never-started row hashes the SAME value on both sides of a comparison, so it
        # cannot churn — which is why it needs no guard of its own.
        self.assertEqual(
            term({"name": "T", "session": "", "liveness": dead}),
            1,
            "a never-started row must hash the death term (constant, so it moves nothing)",
        )

        # Every ALIVE verdict collapses to the same value — that IS the churn fix.
        for shape in (
            {"name": "T", "session": "s-1"},
            {"name": "T", "session": "", "sessions": ["s-1"]},
        ):
            for verdict in (self.m.LIVENESS_LIVE, self.m.LIVENESS_PARKED):
                self.assertEqual(
                    term(dict(shape, liveness=verdict)),
                    0,
                    "%s did not collapse to the alive term" % verdict,
                )

    def test_a_pre_collapse_stored_digest_reports_one_change_then_heals(self):
        """The consequence EVERY subject hits at once on deploy, and it is fail-open.

        The liveness term's SHAPE changed (the raw word -> a bare `0`/`1`) and the stored
        record carries no version, so `load_stored` returns the old string and
        `digest_moved` fires once with nothing about the tree moved. Pinned because a
        one-shot spurious CHANGE is indistinguishable from a real one in a log, and
        because the healing half -- `--save` rewriting the digest in the new format -- is
        what bounds it to exactly once per subject.
        """
        self.prime("ATopic")
        state = json.loads(self.read_state("ATopic"))
        self.assertIn("digest", state)
        state["digest"] = "0" * 64  # no build computes this — stands in for the old shape
        with open(self.m.state_path("ATopic"), "w", encoding="utf-8") as fh:
            json.dump(state, fh)

        rc, out = self.check("ATopic")
        self.assertEqual(
            rc, self.m.EXIT_CHANGE, "a pre-collapse digest did not report a change"
        )

        self.save("ATopic")
        rc, out = self.check("ATopic")
        self.assertEqual(rc, self.m.EXIT_NOCHANGE, out)

    def test_an_id_set_only_rows_death_moves_the_digest(self):
        """The shape the unguarded term exists for, driven through the REAL path.

        `read_task` sets `session` from the frontmatter `claude_session_id` alone while
        `sessions` carries the whole id set, so a row whose ids live only in
        `metrics_sessions` reaches the digest with `session == ""`. An earlier cut keyed
        the term on `session` and silently stopped detecting this row's death — a shape
        this module measures as common (64 of 124 rows carrying a `metrics_sessions`
        block have no `claude_session_id`). This drives `evaluate` -> `liveness_of` ->
        `digest_of` rather than the term alone, so that regression is reproducible here
        and not only in the docstring.
        """
        self.registry("s-set")
        live = self.digest_with_metrics_only("s-set")
        os.remove(os.path.join(self.m.REGISTRY_DIR, "1.json"))
        dead = self.digest_with_metrics_only("s-set")
        self.assertNotEqual(
            live, dead, "an id-set-only row's death did not move the digest"
        )

    def test_an_unenriched_row_defaults_to_the_death_term(self):
        """The default is deliberate, not an oversight — and it is the OPPOSITE direction
        from the session-less guard above, which is why it is a test of its own.

        An unenriched row *with* a session id hashes the DEATH term: `liveness` defaults
        to `LIVENESS_NONE`, and with a session id present that IS the death verdict. The
        sole production call site (`evaluate`) always enriches first, so this is a fixture
        path — pinned anyway, because it is the value any caller that skips enrichment
        gets, and `digest_of`'s docstring names it.
        """
        self.assertEqual(
            self.m.liveness_change_term({"name": "T", "session": "s-gone"}),
            1,
            "an unenriched row did not default to the death term",
        )

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

    # -- the id set, and the roster-name fallback ---------------------------- #

    def registry_named(self, sid, name, status="idle"):
        """A registry record carrying a session `name` — the roster the fallback reads."""
        with open(os.path.join(self.m.REGISTRY_DIR, "1.json"), "w") as fh:
            json.dump(
                {
                    "sessionId": sid,
                    "pid": os.getpid(),
                    "procStart": live_proc_start(os.getpid()),
                    "status": status,
                    "name": name,
                },
                fh,
            )

    def row_for(self, rel):
        """Read one task file through the gate's own `read_task`."""
        return self.m.read_task(os.path.join(self.vault, rel))

    def test_empty_id_set_with_a_live_roster_name_is_not_unowned(self):
        """The roster-name fallback — the case the gate renders unowned today.

        An `in_progress` row whose worker never stamped an id has an EMPTY id set, so
        there is no id to put to the registry or to the heartbeat and a `none` verdict
        says nothing about ownership. The row's own title is the subordinate fallback:
        a live roster entry whose label matches it marks the row OWNED.

        Measured 2026-09-30: two workers spawned at 12:39 parked on their first tool
        call, so neither task file ever took an id; at the 13:00 sweep both rows
        rendered ready-to-start while each held a live roster entry whose label
        exactly matched the task title — and the standing spawn mandate opens such a
        row rather than merely listing it.
        """
        self.registry_named("s-live", name="ATask")
        tracked = [{"name": "ATask", "session": ""}]
        self.m.enrich_liveness(tracked, self.m.read_registry(), self.m.read_feed())
        self.assertEqual(tracked[0]["liveness"], self.m.LIVENESS_LIVE)

    def test_empty_id_set_with_a_non_matching_roster_name_stays_unowned(self):
        """The NEGATIVE CONTROL — the fallback is one-directional.

        A roster is empty-not-absence, so a MISS proves nothing and the row must stay
        ready-to-start exactly as it does today. A build that marks any empty-id row
        owned would block every legitimate spawn, so the positive test above is not
        sufficient on its own.
        """
        self.registry_named("s-live", name="Some Other Task Entirely")
        tracked = [{"name": "ATask", "session": ""}]
        self.m.enrich_liveness(tracked, self.m.read_registry(), self.m.read_feed())
        self.assertEqual(tracked[0]["liveness"], self.m.LIVENESS_NONE)

    def test_an_id_in_metrics_sessions_marks_the_row_owned(self):
        """The id set is `claude_session_id` PLUS every `metrics_sessions` id.

        A row whose `claude_session_id` was never stamped but whose `metrics_sessions`
        holds a live id is OWNED, and the roster fallback — which applies only when the
        set is EMPTY — must not be reached. Measured 2026-10-01 in this vault: of the
        124 tasks carrying a `metrics_sessions` block, 64 carry no `claude_session_id`
        at all, so a build reading that one field leaves all 64 reading `none`.
        """
        self.write("25 Tasks/ASetTask.md", TASK_WITH_METRICS.format(ids="s-live"))
        self.registry("s-live")
        row = self.row_for("25 Tasks/ASetTask.md")
        self.m.enrich_liveness([row], self.m.read_registry(), self.m.read_feed())
        self.assertEqual(row["liveness"], self.m.LIVENESS_LIVE)

    def test_the_empty_id_set_still_falls_back_when_metrics_sessions_is_absent(self):
        """A row with no `metrics_sessions` key at all is the empty set, not a crash."""
        self.registry_named("s-live", name="ATask")
        tracked = [{"name": "ATask", "session": ""}]
        self.m.enrich_liveness(tracked, self.m.read_registry(), self.m.read_feed())
        self.assertEqual(tracked[0]["liveness"], self.m.LIVENESS_LIVE)


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
        """No payload to read means no table to store: the save refuses rather than
        clobbering a good snapshot, and the record is left exactly as it was."""
        self.save("ATopic", "good table\n")
        before = self.read_state("ATopic")
        os.remove(self.m.payload_path("ATopic"))
        rc, _ = self.run_gate("--subject", "ATopic", "--save")
        self.assertEqual(rc, self.m.EXIT_CHANGE)
        self.assertEqual(self.read_state("ATopic"), before)

    def test_a_table_on_stdin_is_refused_and_no_record_is_written(self):
        """A caller-supplied table is refused, never recorded.

        A caller that hands the table over itself moves `recorded_at` to save time, so a
        stale table reads as fresh and the next tick replays it. The supported path is
        `--write-payload`; a piped table is a usage error, and a refused save leaves no
        record at all rather than a half-written one.
        """
        rc, out = self.run_gate("--subject", "ATopic", "--write-payload", stdin="old\n")
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        rc, _ = self.run_gate("--subject", "ATopic", "--save", stdin="caller table\n")
        self.assertEqual(rc, self.m.EXIT_USAGE)
        self.assertFalse(
            os.path.exists(self.m.state_path("ATopic")),
            "a refused save must leave no record at all",
        )

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
        rc, out = self.run_gate("--subject", "No Such Subject", "--save")
        self.assertEqual(rc, self.m.EXIT_CHANGE, out)
        self.assertIn("CHANGE fail-open:", out)


class TestBoundedStdin(Base):
    """A stdin that never EOFs must not stall `--save`.

    Every other test here drives the gate through `run_gate`, which swaps `sys.stdin` for
    an `io.StringIO` — and a StringIO always EOFs, so the suite is structurally unable to
    reproduce the stall. These use a real pipe instead: one with its write end held open
    (the defect), one closed immediately (the control).

    Measured 2026-09-30 against the live Manager Layer store: the open-pipe shape ran
    45 013 ms and was killed, against 654 ms for `< /dev/null`, and left the store
    byte-for-byte unchanged because the save never ran.
    """

    def gate_with_pipe(self, hold_open, *argv):
        """Run the gate with fd-backed stdin. -> (rc, stdout+stderr, elapsed seconds)."""
        import sys

        r, w = os.pipe()
        if not hold_open:
            os.close(w)
            w = None
        old_in, old_out, old_err = sys.stdin, sys.stdout, sys.stderr
        sys.stdin, sys.stdout, sys.stderr = os.fdopen(r), io.StringIO(), io.StringIO()
        started = time.monotonic()
        try:
            rc = self.m.main(list(argv) + ["--vault", self.vault])
            out = sys.stdout.getvalue() + sys.stderr.getvalue()
        finally:
            elapsed = time.monotonic() - started
            sys.stdin.close()
            sys.stdin, sys.stdout, sys.stderr = old_in, old_out, old_err
            if w is not None:
                os.close(w)
        return rc, out, elapsed

    def test_a_never_eof_stdin_is_abandoned_by_its_own_named_code(self):
        """The bound holds, and the code is the run's own — not a kill's, not a verdict.

        SC4's distinction, asserted directly: exit 3 is neither the gate's "changed"
        (10) nor its "usage error" (2), and the save leaves no record behind.
        """
        self.m.STDIN_WAIT_SECONDS = 0.2
        rc, out, elapsed = self.gate_with_pipe(True, "--subject", "ATopic", "--save")
        self.assertEqual(rc, self.m.EXIT_STDIN_TIMEOUT, out)
        self.assertNotEqual(rc, self.m.EXIT_CHANGE, "must not read as the success verdict")
        self.assertNotEqual(rc, self.m.EXIT_USAGE)
        self.assertIn("no EOF", out)
        self.assertLess(elapsed, 5.0, "the bound must hold, not the pipe's lifetime")
        self.assertFalse(
            os.path.exists(self.m.state_path("ATopic")),
            "a save that never ran must leave no record at all",
        )

    def test_the_control_a_closed_pipe_still_saves(self):
        """Negative control: the guard must not be satisfied by refusing every save.

        Same pipe, write end closed — a real EOF on a real fd — so the bound must not
        fire and the save must take the supported path and write the record.
        """
        self.m.STDIN_WAIT_SECONDS = 0.2
        rc, out = self.run_gate("--subject", "ATopic", "--write-payload", stdin="Subject: x\n")
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        rc, out, _ = self.gate_with_pipe(False, "--subject", "ATopic", "--save")
        self.assertNotEqual(rc, self.m.EXIT_STDIN_TIMEOUT, out)
        self.assertTrue(os.path.exists(self.m.state_path("ATopic")), out)


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

    def test_save_names_both_provenance_paths_labelled(self):
        """The two halves the drive leg's dispatch takes from *different* producers.

        `--save` writes the store while the gate writes the snapshot, and the two paths
        sit one directory apart and read alike — so the caller once took the wrong half
        (measured 2026-09-30: the store's `recorded_at` passed where the snapshot's was
        required). Each label names where its half lands, which is the whole deliverable,
        so it is pinned here rather than left to the tolerant `assertIn("SAVED", out)` —
        that assertion passes with both lines deleted.
        """
        rc, out = self.save("ATopic")
        self.assertEqual(rc, self.m.EXIT_CHANGE)
        self.assertIn("store:    %s" % self.m.state_path("ATopic"), out)
        self.assertIn(
            "snapshot: %s" % self.m.loop_snapshot_path(self.vault, "ATopic"), out
        )
        self.assertIn("(recorded_at source)", out)
        # No `--buckets` staged here, so the store half is absent by design and the
        # label says so rather than asserting a half the file does not carry.
        self.assertIn("no bucket sets staged this tick", out)

    def test_save_names_both_paths_on_the_no_change_branch_too(self):
        """The no-change branch carries the same two lines.

        Without this, deleting that branch's `print_provenance` call passes green — the
        same hole the sibling test's docstring names for `assertIn("SAVED", out)`, one
        branch over. The no-change tick is the common one, so it is the branch a reader
        is most likely to meet.
        """
        self.save("ATopic")
        rc, out = self.save("ATopic")
        self.assertEqual(rc, self.m.EXIT_NOCHANGE)
        self.assertIn("store:    %s" % self.m.state_path("ATopic"), out)
        self.assertIn(
            "snapshot: %s" % self.m.loop_snapshot_path(self.vault, "ATopic"), out
        )


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

    def snapshot(self, subject, names, recorded_at=None):
        d = os.path.join(os.environ["SWEEP_GATE_BASE"], os.path.basename(self.vault).lower())
        os.makedirs(d, exist_ok=True)
        payload = {"tasks": {n: {} for n in names}}
        if recorded_at is not None:
            payload["recorded_at"] = recorded_at
        with open(
            os.path.join(d, "%s.snapshot.json" % self.m.slug(subject)), "w", encoding="utf-8"
        ) as fh:
            json.dump(payload, fh)

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

    def test_write_buckets_refuses_a_single_key_mapped_to_an_empty_list(self):
        """The vacuous-truth hole, in its one-key form — and the case is now refused by a
        DIFFERENT branch than the one it was written for. `all(...)` over `[]` is True, so
        `{"done": []}` once passed the very check whose message said "must map to a
        non-empty list of names", staged at exit 0 and was saved, leaving the drive leg
        holding its whole ready batch at clause (0).

        Under direction (2) an empty list is *accepted* per bucket — that is the whole
        reversal — so this payload clears the per-bucket check and is refused by the
        **all-empty** branch instead: a set whose every value is `[]` cannot come from a
        non-empty tracked set. The thirteen-key form of the same case is
        `TestBucketVocabulary.test_an_all_empty_set_is_still_refused`."""
        rc, _ = self.buckets("ATopic", {"done": []})
        self.assertEqual(rc, self.m.EXIT_USAGE)

    def test_write_buckets_accepts_one_empty_bucket_among_full_ones(self):
        """The mixed case — and the direction-(2) REVERSAL of the contract this test used to
        pin. A bucket mapped to `[]` is now a declaration of absence, "evaluated, nothing in
        it", which is the reading the store could not previously express: a sparse map left
        "bucket empty" and "bucket not evaluated" indistinguishable, and nothing in the
        record said which.

        ⚠️ The guard this replaces is a real loss, recorded rather than glossed. The old
        refusal caught a partial mis-parse that emitted a key with an empty list. The new
        completeness half catches a mis-parse that OMITS a key; it cannot catch one that
        emits the key empty. The trade is deliberate — declared means evaluated — and the
        all-empty refusal still stands, because a non-empty tracked set always populates at
        least one bucket."""
        rc, out = self.buckets("ATopic", full_set(done=["ATask"], stuck=[]))
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)

    def test_a_corrected_restage_lands_on_an_unchanged_tree(self):
        """The no-heal, and the negative control for it. `digest_of` covers the tracked set
        only, so a corrected set staged against an unchanged tree used to return
        `SAVED no-change (digest equal)` at exit 0 and never reach the store — leaving the
        bad record in place until the tree next moved. The bucket half is now a save input
        in its own right, so the second save below writes even though the digest is equal.
        """
        first = full_set(done=["ATask"])
        rc, out = self.buckets("ATopic", first)
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        rc, out = self.save_with_table("ATopic", "t\n", "--buckets", out.strip())
        self.assertEqual(rc, self.m.EXIT_CHANGE, out)
        self.assertEqual(json.loads(self.read_state("ATopic"))["bucket_sets"], first)

        # Same tree, corrected classification: the digest is equal, the half moved.
        second = full_set(done=["ATask"], stuck=["AGoalTask"])
        rc, out = self.buckets("ATopic", second)
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        rc, out = self.save_with_table("ATopic", "t\n", "--buckets", out.strip())
        self.assertEqual(rc, self.m.EXIT_CHANGE, out)
        self.assertEqual(json.loads(self.read_state("ATopic"))["bucket_sets"], second)

    def test_a_save_with_no_buckets_on_an_unchanged_tree_still_writes_nothing(self):
        """The other side of the same decision, so the new clause cannot pass by writing
        unconditionally: with no `--buckets` and an equal digest, the record is untouched —
        the documented no-change contract, which is correct and must survive."""
        self.save("ATopic")
        before = self.read_state("ATopic")
        rc, out = self.run_gate("--subject", "ATopic", "--save")
        self.assertEqual(rc, self.m.EXIT_NOCHANGE, out)
        self.assertEqual(self.read_state("ATopic"), before)

    def test_save_records_the_bucket_sets_under_the_key(self):
        payload = {**full_set(), "done": ["ATask"], "ready-to-start": ["AGoalTask"]}
        rc, out = self.buckets("ATopic", payload)
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        rc, out = self.save_with_table("ATopic", "t\n", "--buckets", out.strip())
        self.assertEqual(rc, self.m.EXIT_CHANGE, out)
        self.assertEqual(
            json.loads(self.read_state("ATopic"))["bucket_sets"],
            payload,
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
        rc, out = self.save_with_table(
            "ATopic", "t\n", "--buckets", os.path.join(self.tmp, "nope.json")
        )
        self.assertEqual(rc, self.m.EXIT_USAGE, out)

    def test_save_refuses_a_staged_file_that_cannot_gate(self):
        """The second door. `--save --buckets <path>` reads a file directly, so validating
        only `--write-buckets` left a hand-written or stale staging file able to reach
        `save_stored` with an all-empty set — the same defect by the other route."""
        p = os.path.join(self.tmp, "stale.buckets.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"done": [], "stuck": []}, fh)
        rc, out = self.save_with_table("ATopic", "t\n", "--buckets", p)
        self.assertEqual(rc, self.m.EXIT_USAGE, out)
        self.assertFalse(
            os.path.exists(self.m.state_path("ATopic")),
            "a refused save must leave no record at all",
        )

    def test_save_refuses_a_staged_file_that_omits_a_declared_bucket(self):
        """The second door's COMPLETENESS half — the case the test above does not reach.

        `test_save_refuses_a_staged_file_that_cannot_gate` stages `{"done": [], "stuck": []}`,
        which the **all-empty** branch refuses, so the omission check never runs there. Both
        doors resolve `declared` and share `bucket_shape_error`, so the behaviour matches
        `--write-buckets` — but this is the path a stale or hand-written staging file reaches,
        and nothing pinned it.

        The payload is otherwise valid on purpose: every one of its twelve keys is declared,
        and one carries a row, so **omission is its only defect** and the exit code
        discriminates. A sparse set is exactly what the live producer wrote before
        2026-10-04 (6 of 13 declared names present)."""
        payload = full_set(done=["ATask"])
        del payload["stuck"]
        p = os.path.join(self.tmp, "sparse.buckets.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        rc, out = self.save_with_table("ATopic", "t\n", "--buckets", p)
        self.assertEqual(rc, self.m.EXIT_USAGE, out)
        self.assertFalse(
            os.path.exists(self.m.state_path("ATopic")),
            "a refused save must leave no record at all",
        )

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

    # -- the discriminator -----------------------------------------------------------
    #
    # The `ATopic` fixture declares `## Goals: - [[AGoal]] - [[ATask]]`, so the declared
    # membership is exactly `{AGoal, ATask}` and a caller-only name inside it is the known
    # healthy asymmetry rather than a defect.

    def test_compare_renders_the_healthy_asymmetry_without_a_warning(self):
        """The shape a correct tick produces: caller-only is within the declared membership
        the snapshot does not hold, gate-only is empty. Before the discriminator this
        printed `⚠️ DIVERGENCE`, so a healthy tick and a broken one were one line."""
        self.tracked("ATopic", ["ATask", "AGoal"])
        self.snapshot("ATopic", ["ATask"])
        rc, out = self.run_gate("--subject", "ATopic", "--compare-tracked")
        self.assertEqual(rc, self.m.EXIT_NOCHANGE, out)
        self.assertNotIn("DIVERGENCE", out)
        self.assertIn("healthy", out)
        # The reading line prints *before* the healthy early-return, so it is on this path
        # too — pinned here, or it could be deleted or broken without a test noticing.
        self.assertIn("reading:", out)

    def test_compare_names_the_caller_side_when_the_scan_dropped_a_name(self):
        """Tick 41's shape: the caller's own set is exactly the healthy asymmetry, and the
        gate carries one name it lacks — so the caller's scan is the side to check. The old
        line named neither side, and the manager set about investigating a real membership
        disagreement that did not exist."""
        self.tracked("ATopic", ["ATask", "AGoal"])
        self.snapshot("ATopic", ["ATask", "Dropped"])
        rc, out = self.run_gate("--subject", "ATopic", "--compare-tracked")
        self.assertEqual(rc, self.m.EXIT_DIVERGENT, out)
        self.assertIn("caller-scan-suspect", out)
        self.assertIn("gate-only: Dropped", out)

    def test_compare_flags_a_disjoint_pair_as_a_caller_format_error(self):
        """Work Approval tick #90: 11 names a side, sharing none. Two independently-derived
        memberships cannot each hold every name the other lacks, so the shape is itself the
        tell — a caller-side format error (here a `sed` that never fired and kept each
        bullet's `— description` tail), not a disagreement of any size."""
        self.tracked("ATopic", ["ATask", "AGoal"])
        self.snapshot("ATopic", ["Other1", "Other2"])
        rc, out = self.run_gate("--subject", "ATopic", "--compare-tracked")
        self.assertEqual(rc, self.m.EXIT_DIVERGENT, out)
        self.assertIn("caller-format", out)
        self.assertIn("format error", out)

    def test_compare_names_the_caller_side_when_the_scan_overshot(self):
        """The other direction of the same cause: gate-only is empty and the caller carries
        a name the page never declared, so the scan is too large — or the gate's snapshot is
        a refresh behind. Both readings are stated, because the shape cannot tell them
        apart."""
        self.tracked("ATopic", ["ATask", "Stray"])
        self.snapshot("ATopic", ["ATask"])
        rc, out = self.run_gate("--subject", "ATopic", "--compare-tracked")
        self.assertEqual(rc, self.m.EXIT_DIVERGENT, out)
        self.assertIn("caller-overshoot", out)
        self.assertIn("refresh behind", out)

    def test_the_same_signature_pair_renders_two_different_cause_labels(self):
        """SC1's probe, and why a counts-only change cannot satisfy it. Both cases below are
        `gate-only 1`, so a report that merely appends the counts renders them identically.
        They differ only in whether the caller's own extra name stayed inside the page's
        declaration — and that is what has to reach the label."""
        self.tracked("ATopic", ["ATask", "AGoal"])
        self.snapshot("ATopic", ["ATask", "Extra"])
        rc_dropped, out_dropped = self.run_gate("--subject", "ATopic", "--compare-tracked")

        self.tracked("ATopic", ["ATask", "Stray"])
        rc_moved, out_moved = self.run_gate("--subject", "ATopic", "--compare-tracked")

        for out in (out_dropped, out_moved):
            self.assertIn("gate-only 1", out)
        self.assertEqual(rc_dropped, self.m.EXIT_DIVERGENT, out_dropped)
        self.assertEqual(rc_moved, self.m.EXIT_DIVERGENT, out_moved)
        self.assertIn("caller-scan-suspect", out_dropped)
        self.assertIn("both-sides-moved", out_moved)
        self.assertNotEqual(out_dropped, out_moved)

    def test_compare_prints_the_two_clocks_so_a_stale_snapshot_is_visible(self):
        """Cause (2) — the gate's snapshot a refresh behind — renders the same shape as a
        too-large scan, so the report cannot claim it. It can make it *visible*, which is
        what this line is for: the snapshot's own `recorded_at` and the mtime of the
        caller's scan, both printed rather than assumed."""
        self.tracked("ATopic", ["ATask", "Stray"])
        self.snapshot("ATopic", ["ATask"], recorded_at="2026-10-03T13:25:00+02:00")
        rc, out = self.run_gate("--subject", "ATopic", "--compare-tracked")
        self.assertEqual(rc, self.m.EXIT_DIVERGENT, out)
        self.assertIn("gate snapshot last digest change 2026-10-03T13:25:00+02:00", out)
        self.assertIn("caller scan written", out)

    def test_compare_says_unclassified_when_the_declaration_cannot_be_read(self):
        """"I could not check" must not read as a verdict. With the page's `## Goals`
        section gone the shape cannot be classified at all, and the report says so rather
        than reaching for the nearest label — the same rule the fail-open branch above
        carries for an unreadable snapshot."""
        self.write("23 Topics/ATopic.md", "---\npage_type: topic\n---\n\n# Scope\n")
        self.tracked("ATopic", ["ATask", "Stray"])
        self.snapshot("ATopic", ["ATask", "Extra"])
        rc, out = self.run_gate("--subject", "ATopic", "--compare-tracked")
        self.assertEqual(rc, self.m.EXIT_DIVERGENT, out)
        self.assertIn("unclassified", out)
        self.assertIn("could not be read", out)


HELD = "aaaaaaaa-1111-2222-3333-444444444444"
OTHER = "bbbbbbbb-1111-2222-3333-444444444444"


class HoldDigest(Base):
    """A hold is a DIGEST INPUT here, never a suppression.

    This gate decides whether the sweep runs at all, so suppressing it for a held session
    would stop the sweep and the held row would never render -- the failure the hold design
    forbids, where a row that disappears from a sweep is indistinguishable from a row that
    got fixed. Including the hold in the digest is what makes a hold being ADDED or
    RELEASED wake the loop so the row re-renders.

    Both directions named. The too-tight case (a hold on a tracked session moves the
    digest) and the too-loose case (a hold on a session NOT in the tracked set leaves it
    alone) fail in opposite directions, and only one of them looks like a bug.
    """

    def setUp(self):
        super().setUp()
        self.holds = os.path.join(self.tmp, "holds.json")
        self.m.HOLDS_PATH = self.holds
        self.write_holds()

    def write_holds(self, *session_ids):
        entries = ", ".join(
            '"%s": {"reason": "operator: leave it"}' % sid for sid in session_ids
        )
        with open(self.holds, "w", encoding="utf-8") as handle:
            handle.write('{"version": 1, "holds": {%s}}' % entries)

    def row(self, name, session):
        return {
            "name": name,
            "status": "in_progress",
            "phase": "execution",
            "session": session,
        }

    def test_fires_on_a_held_session(self):
        """Too-tight: adding a hold on a tracked session MOVES the digest."""
        tracked = [self.row("a", HELD)]
        before = self.m.digest_of(tracked)
        self.write_holds(HELD)
        self.assertNotEqual(before, self.m.digest_of(tracked))

    def test_does_not_fire_on_a_clean_session(self):
        """Too-loose: a hold on a session NOT in the tracked set leaves the digest alone."""
        tracked = [self.row("a", OTHER)]
        before = self.m.digest_of(tracked)
        self.write_holds(HELD)
        self.assertEqual(before, self.m.digest_of(tracked))

    def test_releasing_a_hold_moves_the_digest(self):
        """The pair that matters: release must wake the loop too, not only hold."""
        tracked = [self.row("a", HELD)]
        self.write_holds(HELD)
        before = self.m.digest_of(tracked)
        self.write_holds()
        self.assertNotEqual(before, self.m.digest_of(tracked))

    def test_a_corrupt_store_reads_as_nothing_held(self):
        """Fail-open: an unreadable store must not move the digest."""
        tracked = [self.row("a", HELD)]
        before = self.m.digest_of(tracked)
        with open(self.holds, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(before, self.m.digest_of(tracked))


class TestStuck(Base):
    """`apply_stuck` — BOTH branches of the runbook's rule.

    ⚠️ These call `apply_stuck` DIRECTLY with seeded inputs, and that is the point
    rather than a style choice. The bucket-level cases elsewhere inject `stuck`,
    which is green on a build implementing NEITHER branch, so none of them can tell
    branch (a) from branch (b). Seeding the registry and the busy-since map is what
    makes these fail before the fix: an idle session is not `busy`, so a busy-only
    `apply_stuck` set `stuck = False` and the positive case could not pass.

    ⚠️ `test_branch_a_still_fires` is load-bearing. The fix ADDS the missing limb;
    an implementation that replaces (a) with (b) satisfies every (b) case here and
    is caught only by that one.
    """

    NOW = 1_800_000_000.0
    IDLE = {"sid": {"alive": True, "status": "idle"}}
    BUSY = {"sid": {"alive": True, "status": "busy"}}

    def row(self, **kw):
        # `session` is not decoration: `apply_stuck` reads `registry.get(t["session"])`
        # and an empty one resolves to None, so the positive case would fail on a
        # CORRECT build too. `met` is the `n/m` string `checkbox_count` emits, not a
        # count — `"0/1"` is one open box.
        d = {
            "name": "S",
            "status": "in_progress",
            "phase": "execution",
            "session": "sid",
            "goals": [],
            "progress_hash": "ph",
            "met": "0/1",
            "mtime": self.NOW - self.m.STUCK_SECONDS - 60,
        }
        d.update(kw)
        return d

    def stuck(self, row, registry, prev=None):
        self.m.apply_stuck([row], registry, prev or {}, self.NOW)
        return row["stuck"]

    def test_branch_b_flags_an_idle_row_holding_an_open_box(self):
        self.assertTrue(self.stuck(self.row(), self.IDLE))

    def test_branch_b_respects_the_threshold(self):
        # The negative control. Without it "mark every idle execution row stuck"
        # passes the positive case, and the ~30 min qualifier is not a rule at all.
        self.assertFalse(
            self.stuck(self.row(mtime=self.NOW - self.m.STUCK_SECONDS + 60), self.IDLE)
        )

    def test_branch_b_exempts_a_parked_row(self):
        # The runbook's WAITING rule: a row held from OUTSIDE the worker is not stuck,
        # and the file-unchanged proxy cannot tell it from a wedged one.
        self.assertFalse(self.stuck(self.row(liveness=self.m.LIVENESS_PARKED), self.IDLE))

    def test_branch_b_requires_execution_phase(self):
        self.assertFalse(self.stuck(self.row(phase="planning"), self.IDLE))

    def test_branch_b_requires_an_open_box(self):
        self.assertFalse(self.stuck(self.row(met="1/1"), self.IDLE))
        self.assertFalse(self.stuck(self.row(met="—"), self.IDLE))

    def test_branch_b_without_an_mtime_is_not_stuck(self):
        # A row whose file could not be stat'd must not be guessed onto the ⚠️ row.
        self.assertFalse(self.stuck(self.row(mtime=None), self.IDLE))

    def test_branch_a_still_fires(self):
        # phase/met/mtime are deliberately outside branch (b)'s reach, so a pass here
        # cannot be branch (b) answering instead.
        prev = {"S": {"since": self.NOW - self.m.STUCK_SECONDS - 60, "progress": "ph"}}
        self.assertTrue(
            self.stuck(self.row(phase="planning", met="—", mtime=None), self.BUSY, prev)
        )

    def test_branch_a_resets_on_a_progress_write(self):
        prev = {"S": {"since": self.NOW - self.m.STUCK_SECONDS - 60, "progress": "OTHER"}}
        self.assertFalse(
            self.stuck(self.row(phase="planning", met="—", mtime=None), self.BUSY, prev)
        )

    def test_read_task_carries_the_file_mtime(self):
        # Branch (b)'s only input, and the one `apply_stuck` cannot derive itself.
        p = os.path.join(self.vault, "25 Tasks", "ATask.md")
        self.assertAlmostEqual(self.m.read_task(p)["mtime"], os.stat(p).st_mtime, places=3)


class TestVerdictsCache(Base):
    """The verdicts cache's writer — the half clause (1) delegated to the caller in prose.

    Clause (1) (`agents/manager-drive.md`) names the cache's home and assigns the write to
    the *caller* ("The write is the caller's … let the caller persist them"), but nothing in
    the shipped tree wrote the file: the leg holds no write tool, and the two sibling
    subjects' caches are one-off writes whose mtimes never move while their subject ticks
    daily. The store's own half has a code writer (`--write-buckets`); this is its
    counterpart.
    """

    def verdicts(self, subject, payload):
        return self.run_gate(
            "--subject", subject, "--write-verdicts", stdin=json.dumps(payload)
        )

    def read_verdicts(self, subject):
        with open(self.m.verdicts_path(subject), encoding="utf-8") as fh:
            return json.load(fh)

    def test_write_verdicts_lands_beside_the_store_and_prints_its_path(self):
        payload = {
            "ATask": {
                "verdict": "ready",
                "score": 9,
                "content_key": "a" * 64,
                "audited_at": "2026-10-01T10:00:00+02:00",
            }
        }
        rc, out = self.verdicts("ATopic", payload)
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        # The path on stdout is the value the dispatch carries.
        self.assertEqual(out.strip(), self.m.verdicts_path("ATopic"))
        # A sibling of `.buckets.json`, keyed by the same slug — the home clause (1) names.
        self.assertEqual(
            os.path.dirname(self.m.verdicts_path("ATopic")),
            os.path.dirname(self.m.buckets_path("ATopic")),
        )
        self.assertEqual(self.read_verdicts("ATopic"), payload)

    def test_a_null_score_is_a_valid_entry(self):
        """`blocked` rows carry no score — the sibling stores on disk show `"score": null`
        — so a validator demanding an int would refuse a shape the cache actually holds."""
        payload = {
            "ATask": {"verdict": "blocked", "score": None, "content_key": "b" * 64}
        }
        rc, out = self.verdicts("ATopic", payload)
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        self.assertIsNone(self.read_verdicts("ATopic")["ATask"]["score"])

    def test_refuses_an_empty_cache(self):
        rc, _ = self.verdicts("ATopic", {})
        self.assertEqual(rc, self.m.EXIT_USAGE)

    def test_refuses_an_entry_with_no_content_key(self):
        """The invalidation key is the entry's reason to exist: clause (1) re-audits when
        the row file's content changes, so a stored verdict carrying no key can never be
        told from a stale one — and `[]` over the entries would pass vacuously."""
        rc, _ = self.verdicts("ATopic", {"ATask": {"verdict": "ready", "score": 9}})
        self.assertEqual(rc, self.m.EXIT_USAGE)

    def test_refuses_an_entry_with_no_verdict(self):
        rc, _ = self.verdicts("ATopic", {"ATask": {"score": 9, "content_key": "c" * 64}})
        self.assertEqual(rc, self.m.EXIT_USAGE)

    def test_refuses_a_needs_you_entry_with_no_reason(self):
        """The verdicts that quote the audit to the operator require the quote. A cache
        hit carries no dispatch, so an entry stored without its reason can never render
        its `UNFIXABLE:` grounds — the loss the field exists to remove, with the schema
        looking fixed."""
        rc, _ = self.verdicts(
            "ATopic",
            {"ATask": {"verdict": "needs-you", "score": None, "content_key": "e" * 64}},
        )
        self.assertEqual(rc, self.m.EXIT_USAGE)

    def test_refuses_a_blank_reason_on_every_verdict_that_needs_one(self):
        for verdict in self.m.VERDICTS_NEEDING_REASON:
            with self.subTest(verdict=verdict):
                rc, _ = self.verdicts(
                    "ATopic",
                    {
                        "ATask": {
                            "verdict": verdict,
                            "score": None,
                            "content_key": "f" * 64,
                            "reason": "   ",
                        }
                    },
                )
                self.assertEqual(rc, self.m.EXIT_USAGE)

    def test_accepts_a_reason_on_a_needs_you_entry(self):
        payload = {
            "ATask": {
                "verdict": "needs-you",
                "score": None,
                "content_key": "1" * 64,
                "reason": "no Alertmanager is named anywhere in the vault",
            }
        }
        rc, out = self.verdicts("ATopic", payload)
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        self.assertEqual(self.read_verdicts("ATopic"), payload)

    def test_a_reason_is_optional_on_the_other_verdicts(self):
        """The requirement is a property of the *verdict*, not of the schema: a `ready`
        row quotes nothing to the operator, so demanding a reason of it would refuse a
        shape the sibling caches on disk actually hold."""
        payload = {"ATask": {"verdict": "ready", "score": 9, "content_key": "2" * 64}}
        rc, out = self.verdicts("ATopic", payload)
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)

    def test_refuses_a_non_string_reason(self):
        rc, _ = self.verdicts(
            "ATopic",
            {
                "ATask": {
                    "verdict": "ready",
                    "score": 9,
                    "content_key": "3" * 64,
                    "reason": ["not", "a", "string"],
                }
            },
        )
        self.assertEqual(rc, self.m.EXIT_USAGE)

    def seed_verdicts(self, subject, payload):
        """Write a cache directly, the way one written before `reason` existed sits on disk.

        The writer cannot produce that shape any more — that is the point — so a legacy
        entry has to be seeded rather than staged.
        """
        os.makedirs(os.path.dirname(self.m.verdicts_path(subject)), exist_ok=True)
        with open(self.m.verdicts_path(subject), "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    def test_grandfathers_a_carried_forward_entry_with_no_reason(self):
        """⚠️ The rule is on a *fresh audit*, not on the schema. A row written before the
        field existed has no audit left to re-derive its words from, and backfilling is out
        of scope — so an unchanged content_key means carry-forward, and refusing it instead
        made four of five live caches unwritable (measured 2026-10-02)."""
        self.seed_verdicts(
            "ATopic",
            {"ATask": {"verdict": "needs-you", "score": None, "content_key": "a" * 16}},
        )
        rc, out = self.verdicts(
            "ATopic",
            {"ATask": {"verdict": "needs-you", "score": None, "content_key": "a" * 16}},
        )
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        self.assertEqual(
            self.read_verdicts("ATopic")["ATask"]["content_key"], "a" * 16
        )

    def test_refuses_the_same_entry_once_its_content_key_moves(self):
        """The grandfather is keyed on the content key, never on the task name: a moved key
        means the row was re-audited, so the new verdict must carry the auditor's words."""
        self.seed_verdicts(
            "ATopic",
            {"ATask": {"verdict": "needs-you", "score": None, "content_key": "a" * 16}},
        )
        rc, _ = self.verdicts(
            "ATopic",
            {"ATask": {"verdict": "needs-you", "score": None, "content_key": "b" * 16}},
        )
        self.assertEqual(rc, self.m.EXIT_USAGE)

    def test_refuses_a_fresh_entry_on_a_subject_with_no_cache(self):
        """The strict reading still holds where there is genuinely no prior cache: with
        nothing on disk there is no carry-forward to grandfather, so a reason-less
        `needs-you` entry is a fresh audit missing its words."""
        rc, _ = self.verdicts(
            "AFreshTopic",
            {"ATask": {"verdict": "needs-you", "score": None, "content_key": "c" * 16}},
        )
        self.assertEqual(rc, self.m.EXIT_USAGE)

    def test_grandfathering_does_not_leak_to_the_other_verdicts(self):
        """Only `VERDICTS_NEEDING_REASON` is exempted, and only while carried: a `ready`
        row is unaffected either way, and a carried `ready` row still validates normally."""
        self.seed_verdicts(
            "ATopic", {"ATask": {"verdict": "ready", "score": 9, "content_key": "d" * 16}}
        )
        rc, out = self.verdicts(
            "ATopic", {"ATask": {"verdict": "ready", "score": 9, "content_key": "d" * 16}}
        )
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)

    def test_reports_which_rows_it_grandfathered(self):
        """Not a failure and not silent: a reader should see which rows cannot render their
        grounds now, rather than discover it from an empty `UNFIXABLE:` line later."""
        self.seed_verdicts(
            "ATopic",
            {"Legacy": {"verdict": "unfixable", "score": None, "content_key": "e" * 16}},
        )
        rc, _ = self.verdicts(
            "ATopic",
            {"Legacy": {"verdict": "unfixable", "score": None, "content_key": "e" * 16}},
        )
        self.assertEqual(rc, self.m.EXIT_WRITE_OK)
        self.assertEqual(
            self.m.verdicts_grandfathered(
                {"Legacy": {"verdict": "unfixable", "content_key": "e" * 16}},
                {"Legacy": {"content_key": "e" * 16}},
            ),
            ["Legacy"],
        )
        self.assertEqual(self.m.verdicts_grandfathered({}, {}), [])

    def test_refuses_a_non_object(self):
        rc, _ = self.verdicts("ATopic", ["ATask"])
        self.assertEqual(rc, self.m.EXIT_USAGE)

    def test_a_refused_write_leaves_the_previous_cache_untouched(self):
        """The negative control, and the sharper reason this guard exists here: a cache
        that reads back as *present but unusable* is indistinguishable from one never
        written — which is the state this writer exists to end."""
        good = {"ATask": {"verdict": "ready", "score": 9, "content_key": "d" * 64}}
        rc, out = self.verdicts("ATopic", good)
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        rc, _ = self.verdicts("ATopic", {"ATask": {"verdict": "ready", "score": 9}})
        self.assertEqual(rc, self.m.EXIT_USAGE)
        self.assertEqual(self.read_verdicts("ATopic"), good)

    def test_the_cache_is_not_folded_into_the_state_record(self):
        """`save_stored` must not absorb it: the store is the gate's verdict over the
        *tree*, the cache is the leg's verdict over *rows* — different actors, different
        cadences, and clause (1) reads the cache back on the next tick."""
        self.save("ATopic")
        self.assertNotIn("verdicts", json.loads(self.read_state("ATopic")))

    # --- the name half of the key (Finding 2) ----------------------------------------
    #
    # The cache is keyed by task *name*, so the writer is the last place that can stop a
    # name the caller never read in full from becoming a key. Measured 2026-10-02: six of
    # 24 live keys were stale titles, every one of them *invented* by the caller from an
    # `Audit` block that renders names abbreviated with `…` (U+2026) for width. The
    # `content_key` half kept the cache working; the name half drifted on every rename,
    # and the caller is the writer.

    def test_refuses_a_name_carrying_the_abbreviation_marker(self):
        """`…` is a rendering artifact, not a title. A caller holding
        `A Manager's Own Closer Line Is Mirrored…` has not read the row's name, so the
        writer must not accept its guess at the remainder — each such guess is stored under
        a name no file carries, which is the whole of the harm.
        """
        rc, _ = self.verdicts(
            "ATopic",
            {
                "A Manager's Own Closer Line Is Mirrored…": {
                    "verdict": "ready",
                    "score": 9,
                    "content_key": "a" * 64,
                }
            },
        )
        self.assertEqual(rc, self.m.EXIT_USAGE)

    def test_the_abbreviation_marker_never_reaches_the_stored_keys(self):
        """The refusal is total, not a sanitisation: an entry keyed by the abbreviation
        itself is as unusable as one keyed by the guessed expansion, because neither is
        the file's title — and a refused write must not clobber a good cache."""
        good = {"ATask": {"verdict": "ready", "score": 9, "content_key": "d" * 64}}
        rc, out = self.verdicts("ATopic", good)
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        rc, _ = self.verdicts(
            "ATopic",
            {
                "ATask": {"verdict": "ready", "score": 9, "content_key": "d" * 64},
                "A Manager's Own Closer Line Is Mirrored…": {
                    "verdict": "ready",
                    "score": 9,
                    "content_key": "a" * 64,
                },
            },
        )
        self.assertEqual(rc, self.m.EXIT_USAGE)
        stored = self.read_verdicts("ATopic")
        self.assertEqual(stored, good)
        self.assertEqual([k for k in stored if "…" in k], [])

    def test_a_resolvable_name_is_stored_under_the_files_own_basename(self):
        """The other half of the repair: where the name *can* be resolved against the
        tasks dir, the file's own title is the key rather than the caller's spelling of
        it — so case and spacing drift cannot key an entry either."""
        rc, out = self.verdicts(
            "ATopic",
            {"atask": {"verdict": "ready", "score": 9, "content_key": "a" * 64}},
        )
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        self.assertEqual(list(self.read_verdicts("ATopic")), ["ATask"])

    def test_refuses_two_keys_that_resolve_to_one_row(self):
        """`ATask` and `atask` are distinct JSON keys and both name `ATask.md`, so the
        payload does not say which verdict is the row's — and keeping the later one would
        drop the other silently, the same loss this writer refuses everywhere else."""
        rc, _ = self.verdicts(
            "ATopic",
            {
                "ATask": {"verdict": "ready", "score": 9, "content_key": "a" * 64},
                "atask": {"verdict": "blocked", "score": None, "content_key": "b" * 64},
            },
        )
        self.assertEqual(rc, self.m.EXIT_USAGE)

    def test_a_stored_key_that_drifted_in_spelling_still_grandfathers(self):
        """The carry-forward lookup runs against the *stored* cache, so the stored keys are
        normalised through the same rule as the payload. Without that, a legacy entry keyed
        `atask` would read as a fresh audit of `ATask` and be refused for a missing reason —
        the cost the grandfather rule exists to remove, reintroduced by the name fix."""
        self.seed_verdicts(
            "ATopic",
            {"atask": {"verdict": "needs-you", "score": None, "content_key": "a" * 16}},
        )
        rc, out = self.verdicts(
            "ATopic",
            {"ATask": {"verdict": "needs-you", "score": None, "content_key": "a" * 16}},
        )
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        self.assertEqual(list(self.read_verdicts("ATopic")), ["ATask"])

    def test_a_name_that_does_not_resolve_is_stored_exactly_as_sent(self):
        """The fallback the CHANGELOG argues for at length, and the contract most likely to
        regress silently: `--vault` names one vault while a subject's rows may live in
        another (measured: `bro-21389-mdm-via-rest`'s 13 rows are all in
        `seibert-brogrammers/25 Tasks`), so a name the index does not carry is stored
        unchanged rather than refused — refusing would brick every write for those
        subjects, and for this one the moment a row file is deleted."""
        name = "BRO-22002 Split MDM Parties and Contacts via REST"
        rc, out = self.verdicts(
            "ATopic", {name: {"verdict": "ready", "score": 9, "content_key": "a" * 64}}
        )
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        self.assertEqual(list(self.read_verdicts("ATopic")), [name])

    def test_a_name_carrying_a_separator_is_left_as_sent(self):
        """Resolution is an index lookup — `task_index` *is* `os.listdir` of the tasks dir —
        never a stat of a path assembled from the name, so `sub/ATask` is not reduced to
        `ATask`. ⚠️ **The nested file is what makes this load-bearing rather than
        decorative:** with `25 Tasks/sub/ATask.md` absent, a stat-based implementation would
        also store the name verbatim and the test would pass for either mechanism. It is
        present, so only a directory-entry lookup can still answer `sub/ATask`."""
        os.makedirs(os.path.join(self.vault, "25 Tasks", "sub"))
        self.write("25 Tasks/sub/ATask.md", "---\nstatus: next\n---\n")
        rc, out = self.verdicts(
            "ATopic",
            {"sub/ATask": {"verdict": "ready", "score": 9, "content_key": "a" * 64}},
        )
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        self.assertEqual(list(self.read_verdicts("ATopic")), ["sub/ATask"])

    def test_a_filename_sent_with_its_suffix_resolves_to_the_same_row(self):
        """A caller may send the filename rather than the stem. Without the suffix strip the
        lookup asks for `atask.md.md`, misses, and keys the entry `ATask.md` — a name no
        reader will ever match, which matters because the key is the one thing a later tick
        matches a row by."""
        rc, out = self.verdicts(
            "ATopic",
            {"ATask.md": {"verdict": "ready", "score": 9, "content_key": "a" * 64}},
        )
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        self.assertEqual(list(self.read_verdicts("ATopic")), ["ATask"])

    def test_a_two_spelling_stored_cache_still_grandfathers(self):
        """⚠️ This failure was introduced by the repair itself, and it is silent. A legacy
        cache holding two drifted spellings of one row collides; a *strict* re-key of the
        stored side would refuse, the caller discards that reason, the cache is left
        un-rekeyed, `_carried_forward` then misses the canonical title, and a `needs-you`
        entry with no reason is refused — **the write-bricking cost the grandfather rule
        exists to remove, reached by the repair.** `stored` is a lookup source and never a
        destination, so a collision there must be tolerated rather than refused."""
        self.seed_verdicts(
            "ATopic",
            {
                "atask": {"verdict": "needs-you", "score": None, "content_key": "a" * 16},
                "Atask": {"verdict": "ready", "score": 9, "content_key": "b" * 16},
            },
        )
        rc, out = self.verdicts(
            "ATopic",
            {"ATask": {"verdict": "needs-you", "score": None, "content_key": "a" * 16}},
        )
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        self.assertEqual(
            self.read_verdicts("ATopic")["ATask"]["content_key"], "a" * 16
        )

    def test_a_title_containing_ascii_ellipsis_is_not_refused(self):
        """The positive control for the deliberate exclusion: `ABBREVIATION_MARKER` is
        U+2026 alone, because a real title may carry `...`. Widening it would refuse
        exactly the titles the source comment calls common, and nothing else here would
        catch that."""
        rc, out = self.verdicts(
            "ATopic",
            {"ATask... draft": {"verdict": "ready", "score": 9, "content_key": "a" * 64}},
        )
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        self.assertEqual(list(self.read_verdicts("ATopic")), ["ATask... draft"])

    def test_a_marker_bearing_key_in_the_stored_cache_is_benign(self):
        """`stored` is a lookup source only — the write replaces the whole file with the
        payload — so a `…` key already sitting in a cache neither refuses the write nor
        survives it."""
        self.seed_verdicts(
            "ATopic",
            {
                "A Manager's Own Closer Line Is Mirrored…": {
                    "verdict": "ready",
                    "score": 9,
                    "content_key": "a" * 64,
                }
            },
        )
        rc, out = self.verdicts(
            "ATopic",
            {"ATask": {"verdict": "ready", "score": 9, "content_key": "a" * 64}},
        )
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        self.assertEqual(list(self.read_verdicts("ATopic")), ["ATask"])

    def test_the_collision_refusal_leaves_the_previous_cache_untouched(self):
        """The collision path returns before any `open()`, so it cannot clobber today — but
        a reordering that moved it after the shape check or after the write would, and only
        this assertion would catch that."""
        good = {"ATask": {"verdict": "ready", "score": 9, "content_key": "d" * 64}}
        rc, out = self.verdicts("ATopic", good)
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        rc, _ = self.verdicts(
            "ATopic",
            {
                "ATask": {"verdict": "ready", "score": 9, "content_key": "a" * 64},
                "atask": {"verdict": "blocked", "score": None, "content_key": "b" * 64},
            },
        )
        self.assertEqual(rc, self.m.EXIT_USAGE)
        self.assertEqual(self.read_verdicts("ATopic"), good)

    def test_an_unresolvable_name_with_a_suffix_is_still_reduced_to_its_stem(self):
        """The `.md` strip runs before the lookup, so it applies on the unresolvable path
        too: an absent `Foo.md` is stored as `Foo`, canonicalising it exactly as a
        resolvable one is. This is the single sub-case the "left as sent" rule does not
        cover literally, so it is pinned rather than left to the prose."""
        rc, out = self.verdicts(
            "ATopic",
            {"Foo.md": {"verdict": "ready", "score": 9, "content_key": "a" * 64}},
        )
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        self.assertEqual(list(self.read_verdicts("ATopic")), ["Foo"])

    def test_an_unreadable_tasks_dir_says_so_rather_than_degrading_silently(self):
        """`task_index` collapses "unreadable" and "empty" into `{}`, which is right for its
        other callers and wrong here: an empty index stores every name exactly as the caller
        spelled it — the defect this writer's name derivation exists to remove — and without
        the note the write succeeds looking normal. The note is the whole difference between
        a documented fallback and a silent one."""
        tasks_dir = os.path.join(self.vault, "25 Tasks")
        for entry in os.listdir(tasks_dir):
            os.unlink(os.path.join(tasks_dir, entry))
        os.rmdir(tasks_dir)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc, out = self.verdicts(
                "ATopic",
                {"atask": {"verdict": "ready", "score": 9, "content_key": "a" * 64}},
            )
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        self.assertIn("cannot read", err.getvalue())
        self.assertEqual(list(self.read_verdicts("ATopic")), ["atask"])

    def test_a_refusal_names_the_spelling_the_caller_sent(self):
        """Every shape refusal names the DERIVED title, because that is what the shape check
        reads. A caller who sent `atask` would otherwise read a message about a title it
        never typed, and have no way to tell which of its keys was refused."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc, _ = self.verdicts(
                "ATopic", {"atask": {"verdict": "ready", "score": "high"}}
            )
        self.assertEqual(rc, self.m.EXIT_USAGE)
        self.assertIn("'ATask' ← 'atask'", err.getvalue())

    def test_an_empty_tasks_dir_says_so_too(self):
        """⚠️ The case the `OSError` branch does not cover, and it degrades identically: an
        empty-but-readable `25 Tasks` stores every name as the caller spelled it. Guarding
        only the failure mode would make the note a property of *how* the directory failed
        rather than of the outcome — and an empty tasks dir is a real state (a mistyped
        `--vault`, a vault whose layout differs), not a hypothetical."""
        tasks_dir = os.path.join(self.vault, "25 Tasks")
        for entry in os.listdir(tasks_dir):
            os.unlink(os.path.join(tasks_dir, entry))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc, out = self.verdicts(
                "ATopic",
                {"atask": {"verdict": "ready", "score": 9, "content_key": "a" * 64}},
            )
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        self.assertIn("is empty", err.getvalue())
        self.assertEqual(list(self.read_verdicts("ATopic")), ["atask"])

    def test_the_reversed_two_spelling_cache_is_refused_rather_than_silently_wrong(self):
        """The residual is pinned rather than hidden. First spelling wins, so when the
        matching entry *loses* the race the carry-forward misses and the write is
        **refused** — never silently written wrong, and the seeded cache survives intact.
        Unreachable from either writer (every write replaces the whole file, so no cache
        can accumulate two spellings of one row); only a hand-edited cache reaches it, and
        the note on stderr is what makes it visible."""
        self.seed_verdicts(
            "ATopic",
            {
                "Atask": {"verdict": "ready", "score": 9, "content_key": "b" * 16},
                "atask": {"verdict": "needs-you", "score": None, "content_key": "a" * 16},
            },
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc, _ = self.verdicts(
                "ATopic",
                {"ATask": {"verdict": "needs-you", "score": None, "content_key": "a" * 16}},
            )
        self.assertEqual(rc, self.m.EXIT_USAGE)
        self.assertIn("both naming", err.getvalue())
        self.assertEqual(sorted(self.read_verdicts("ATopic")), ["Atask", "atask"])

    def test_a_renamed_row_lands_under_its_current_title(self):
        """The defect the task names: a rename must not strand the entry. The caller reads
        the row's current title off disk, the writer resolves it, and the entry lands under
        that title — the pre-rename key is gone, because the write replaces the cache."""
        rc, out = self.verdicts(
            "ATopic",
            {"ATask": {"verdict": "ready", "score": 9, "content_key": "a" * 64}},
        )
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        os.rename(
            os.path.join(self.vault, "25 Tasks", "ATask.md"),
            os.path.join(self.vault, "25 Tasks", "ATaskRenamed.md"),
        )
        rc, out = self.verdicts(
            "ATopic",
            {"ATaskRenamed": {"verdict": "ready", "score": 9, "content_key": "a" * 64}},
        )
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        self.assertEqual(list(self.read_verdicts("ATopic")), ["ATaskRenamed"])


class TestBucketVocabulary(Base):
    """The vocabulary half of the bucket door — SC1, SC2 and SC4.

    Shape alone accepted any key at all: measured 2026-10-03, `{"hold": ["Alpha"],
    "orphan": ["Beta"], "ready": ["Gamma"]}` staged verbatim at exit 0, and the live store
    carried `backlog` / `yours` against a runbook declaring neither spelling. A set keyed
    by a vocabulary nobody declared gates the drive leg's clause (0) on names no renderer
    agrees with — the drift the runbook's own two-renderer rule forbids.
    """

    # Folded onto the module-level tuple rather than restated: two hardcoded thirteen-name
    # lists in one file meant a future bucket addition was two edits, and nothing tied them
    # together. A `set` here because this class asserts membership; `full_set()` needs the
    # tuple's order for nothing.
    DECLARED = set(DECLARED_NAMES)

    def buckets(self, subject, payload):
        return self.run_gate(
            "--subject", subject, "--write-buckets", stdin=json.dumps(payload)
        )

    # -- direction (2): `[]` is a declaration, and completeness is enforced --- #

    def test_a_declared_bucket_mapped_to_an_empty_list_is_accepted(self):
        """SC1(a). The rule text always demanded "every declared bucket must appear" while
        the writer refused any bucket mapped to an empty list — so the two halves could not
        both hold whenever any bucket was empty, which on the live store is most of them
        (6 of 13 keys present, `recorded_at 2026-10-04T12:39:32+02:00`). Direction (2) makes
        `[]` mean "evaluated, nothing in it"."""
        rc, out = self.buckets("ATopic", full_set(done=["ATask"], stuck=[]))
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)

    def test_a_set_omitting_a_declared_bucket_is_refused(self):
        """SC1(b) — the completeness half. The prose promised it and the code never applied
        it, so a sparse map silently under-declared which buckets were evaluated and
        nothing in the record said which."""
        payload = full_set(done=["ATask"])
        del payload["stuck"]
        rc, out = self.run_both(
            "--subject", "ATopic", "--write-buckets", stdin=json.dumps(payload)
        )
        self.assertEqual(rc, self.m.EXIT_USAGE)
        self.assertIn("stuck", out)

    def test_an_all_empty_set_is_still_refused(self):
        """SC1(c) — the 2026-09-28 vacuous-`all()` hole stays closed. Under direction (2)
        an all-empty set reads as "every bucket evaluated, every one empty", which a
        non-empty tracked set cannot produce: every tracked row lands in some bucket, so at
        least one bucket is non-empty. All-empty therefore remains the mis-parse
        signature rather than a quiet tick."""
        rc, _ = self.buckets("ATopic", {name: [] for name in DECLARED_NAMES})
        self.assertEqual(rc, self.m.EXIT_USAGE)

    def run_both(self, *argv, stdin=""):
        """-> (rc, stdout + stderr). `Base.run_gate` swaps only stdout, and every
        validation message this script emits goes to stderr — so a test asserting the
        refusal NAMES the offending key needs the stream it is actually written to."""
        import sys

        old_in, old_out, old_err = sys.stdin, sys.stdout, sys.stderr
        sys.stdin, sys.stdout, sys.stderr = (
            io.StringIO(stdin),
            io.StringIO(),
            io.StringIO(),
        )
        try:
            rc = self.m.main(list(argv) + ["--vault", self.vault])
            return rc, sys.stdout.getvalue() + sys.stderr.getvalue()
        finally:
            sys.stdin, sys.stdout, sys.stderr = old_in, old_out, old_err

    def temp_runbook(self, text):
        """A stand-in declaration, so a probe never edits the live runbook."""
        p = os.path.join(self.tmp, "temp-runbook.md")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        return p

    # -- SC1: the refusal, at BOTH doors ------------------------------------ #

    def test_write_buckets_refuses_an_off_vocabulary_set(self):
        """The measured payload, and the union pinned in both directions. This exact set
        staged verbatim at exit 0 before the check existed; of its three keys, `orphan` and
        `ready` are declared nowhere and are refused, while `hold` IS a declared non-bucket
        disposition and must survive — a refusal that also rejects it is the failure SC4
        calls worse than no check."""
        rc, out = self.run_both(
            "--subject",
            "ATopic",
            "--write-buckets",
            stdin=json.dumps({"hold": ["Alpha"], "orphan": ["Beta"], "ready": ["Gamma"]}),
        )
        self.assertEqual(rc, self.m.EXIT_USAGE, out)
        for key in ("orphan", "ready"):
            self.assertIn(repr(key), out)
        self.assertNotIn("'hold'", out)
        self.assertFalse(
            os.path.exists(self.m.buckets_path("ATopic")),
            "a refused stage must leave no staging file at all",
        )

    def test_save_refuses_an_off_vocabulary_staged_file(self):
        """The SECOND door. `--save --buckets <path>` reads a file directly, so a check on
        the staging path alone leaves a hand-written or stale file able to reach
        `save_stored` — the same defect the empty-list hole already had, one door over."""
        p = os.path.join(self.tmp, "off-vocab.buckets.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"yours": ["ATask"]}, fh)
        rc, out = self.save_with_table("ATopic", "t\n", "--buckets", p)
        self.assertEqual(rc, self.m.EXIT_USAGE, out)
        self.assertFalse(
            os.path.exists(self.m.state_path("ATopic")),
            "a refused save must leave no record at all",
        )

    def test_a_synonym_is_refused_rather_than_normalised(self):
        """The store has been measured emitting `yours` / `YOURS` / `approval` across
        consecutive ticks. Only the runbook's own token is a member; case-folding or
        emoji-stripping the rest re-admits exactly the drift this check exists to catch."""
        for synonym in ("yours", "YOURS", "approval"):
            rc, out = self.buckets("ATopic", {synonym: ["ATask"]})
            self.assertEqual(rc, self.m.EXIT_USAGE, "%s -> %s" % (synonym, out))

    def test_a_superseded_name_in_the_runbooks_prose_is_not_a_member(self):
        """The regression case the fixture is built for. `problem` and `orphan` are named
        in the runbook's own prose — as renames — and appear nowhere in its declaration. A
        parser that collects words off the line admits them, and a clean fixture with no
        removal note passes against that broken parser."""
        for superseded in ("problem", "orphan"):
            rc, out = self.buckets("ATopic", {superseded: ["ATask"]})
            self.assertEqual(rc, self.m.EXIT_USAGE, "%s -> %s" % (superseded, out))

    # -- SC4: the correct sets still pass ----------------------------------- #

    def test_a_set_of_declared_names_round_trips_with_an_identical_key_set(self):
        """SC1's other half, and why it is a KEY-SET assertion rather than a count: a
        count passes on every drift variant the store has produced, and the runbook's own
        declared set is the only thing the stored keys may equal."""
        payload = {name: ["ATask"] for name in sorted(self.DECLARED)}
        rc, out = self.buckets("ATopic", payload)
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        rc, out = self.save_with_table("ATopic", "t\n", "--buckets", out.strip())
        self.assertEqual(rc, self.m.EXIT_CHANGE, out)
        stored = json.loads(self.read_state("ATopic"))["bucket_sets"]
        self.assertEqual(set(stored), self.DECLARED)

    def test_a_disposition_keyed_set_saves(self):
        """`backlog` is a declared NON-BUCKET DISPOSITION, and the live store keys it for
        real (4 tasks on 2026-10-03). A check built from the runbook's parenthesised bucket
        run alone refuses it — and a refusal that also rejects the correct set is worse
        than none."""
        payload = full_set(done=["ATask"], backlog=["ATask"])
        rc, out = self.buckets("ATopic", payload)
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        rc, out = self.save_with_table("ATopic", "t\n", "--buckets", out.strip())
        self.assertEqual(rc, self.m.EXIT_CHANGE, out)
        self.assertEqual(json.loads(self.read_state("ATopic"))["bucket_sets"], payload)

    # -- SC2: the vocabulary comes from the runbook, and only from it ------- #

    def test_editing_the_declared_list_changes_what_is_refused(self):
        """SC2's observable, and the one probe a hard-coded list cannot pass. The
        declaration is read at runtime, so widening it accepts a key refused a moment ago
        and narrowing it refuses one that was accepted."""
        widened = self.temp_runbook(
            RUNBOOK.replace("/ **close-me**", "/ **close-me** / newbucket")
        )
        rc, out = self.run_gate(
            "--subject",
            "ATopic",
            "--runbook",
            widened,
            "--write-buckets",
            stdin=json.dumps({**full_set(done=["ATask"]), "newbucket": ["ATask"]}),
        )
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)

        narrowed = self.temp_runbook(RUNBOOK.replace(" / **orphaned**", ""))
        rc, out = self.run_both(
            "--subject",
            "ATopic",
            "--runbook",
            narrowed,
            "--write-buckets",
            stdin=json.dumps(full_set(done=["ATask"])),
        )
        self.assertEqual(rc, self.m.EXIT_USAGE, out)
        self.assertIn("orphaned", out)

    def test_the_probe_reads_the_override_not_the_live_declaration(self):
        """The negative control for the test above: with no override the same key is
        refused, so the override is what moved the answer — not a check that had stopped
        reading the declaration at all."""
        rc, out = self.buckets("ATopic", {"newbucket": ["ATask"]})
        self.assertEqual(rc, self.m.EXIT_USAGE, out)

    def test_a_vault_with_no_runbook_is_a_usage_error_not_a_silent_pass(self):
        """Fail-closed. A check that skips when it cannot read the declaration is the
        defect this refusal exists to remove, so the absence is itself the error."""
        os.remove(os.path.join(self.vault, "65 Runbooks", "Manager Session.md"))
        rc, out = self.run_both(
            "--subject",
            "ATopic",
            "--write-buckets",
            stdin=json.dumps({"done": ["ATask"]}),
        )
        self.assertEqual(rc, self.m.EXIT_USAGE, out)
        self.assertIn("no bucket declaration found", out)

    def test_an_unparseable_declaration_is_a_usage_error(self):
        """A runbook that exists but declares nothing is refused, never read as an empty
        vocabulary — an empty set would refuse every key, including the correct ones."""
        runbook = self.temp_runbook("# A runbook carrying no Step 4 declaration at all\n")
        rc, out = self.run_both(
            "--subject",
            "ATopic",
            "--runbook",
            runbook,
            "--write-buckets",
            stdin=json.dumps({"done": ["ATask"]}),
        )
        self.assertEqual(rc, self.m.EXIT_USAGE, out)
        self.assertIn("could not parse the bucket set", out)

    # -- SC1(ii): the parser strips the runbook's emphasis ------------------- #

    def test_a_bolded_declaration_yields_the_plain_vocabulary(self):
        """The clause that makes SC1 discriminate. A criterion satisfied by de-bolding the
        *runbook* leaves the parser exactly as brittle as it was and still passes green —
        the store's keys become canonical because the source document was edited, not
        because the gate was fixed. Requiring plain output from a declaration line that
        CARRIES `**` is what forces the fix into the parser, and what stops a future
        re-bold from silently regressing the store the same way."""
        path = os.path.join(self.vault, "65 Runbooks", "Manager Session.md")
        with open(path, encoding="utf-8") as fh:
            self.assertIn(
                "**ready-to-start**", fh.read(), "the fixture lost its emphasis"
            )
        names, err = self.m.declared_bucket_names(path)
        self.assertIsNone(err)
        self.assertEqual(names, self.DECLARED)
        for name in sorted(names):
            self.assertNotIn("*", name, "emphasis survived into %r" % name)

    # -- SC3: the set is derived from the runbook, never hardcoded ----------- #

    def test_every_name_the_parser_derives_tracks_its_own_line(self):
        """SC3's probe, and the one a hardcoded set cannot pass. Every name the parser
        derives is mutated in turn, on the line it is actually parsed from — every bucket
        name on the `Step 4 — Classify into the full bucket set` run, and the dispositions
        on the `Non-bucket dispositions` line — and the answer must follow. The set is
        mutated whole rather than sampled, and no count is restated here, so a fixture edit
        that adds a bucket cannot shrink the probe silently.
        A fully hardcoded set ignores both lines; a partially hardcoded one ignores
        whichever half it froze, which is why the set is mutated whole rather than
        sampled."""
        for old in sorted(self.DECLARED):
            renamed = old + "-renamed"
            mutated = RUNBOOK.replace(old, renamed, 1)
            self.assertNotEqual(mutated, RUNBOOK, "the fixture does not carry %r" % old)
            # Pin WHICH line moved: a mutation that landed in prose would prove nothing
            # about the declaration the parser reads.
            step4 = mutated.split(BUCKET_MARKER, 1)[1].split("\n", 1)[0]
            dispo = mutated.split(DISPOSITION_MARKER, 1)[1].split("\n", 1)[0]
            self.assertTrue(
                renamed in step4 or renamed in dispo,
                "%r was mutated outside the two declaration lines" % old,
            )
            names, err = self.m.declared_bucket_names(self.temp_runbook(mutated))
            self.assertIsNone(err, "%s -> %s" % (old, err))
            self.assertIn(renamed, names, "%r did not track to %r" % (old, renamed))
            self.assertNotIn(old, names, "%r survived its own mutation" % old)


class TestBlockedByRead(Base):
    """The deterministic `blocked_by` read the sweep reader consumes.

    The defect this pins (measured 2026-10-03): the reader promoted a row to
    `ready-to-start` once its FIRST entry had shipped, ignoring later unmet ones.
    Two shapes must therefore be read in full — a block list of three or more
    with the unmet entry STRICTLY INTERIOR (so neither a two-entry special case
    nor a first-plus-last heuristic can pass), and a YAML single-quoted scalar
    carrying an escaped apostrophe (`''`), which a raw regex returns still-escaped
    and therefore unresolvable.
    """

    def blocker(self, name, status):
        self.write(
            f"25 Tasks/{name}.md",
            f"---\npage_type: task\nstatus: {status}\n---\nTags: [[Task]]\n",
        )

    def verdict(self, fm):
        return self.m.blocked_by_verdict(fm, os.path.join(self.vault, "25 Tasks"))

    def test_escaped_apostrophe_is_unescaped(self):
        # YAML escapes `'` as `''` inside a single-quoted scalar. A raw regex
        # returns `Supervisor''s`, which matches no page — and "no page" reads as
        # UNMET, so a COMPLETED blocker would still render blocked-upstream.
        fm = "blocked_by:\n    - '[[Add a Cluster Spawn Mode to the Supervisor''s spawn_agent]]'\n"
        self.assertEqual(
            self.m.fm_wikilinks(fm, "blocked_by"),
            ["Add a Cluster Spawn Mode to the Supervisor's spawn_agent"],
        )

    def test_escaped_apostrophe_is_unescaped_for_goals_too(self):
        # `unescape_scalar` sits inside the shared `fm_wikilinks`, so the widening to
        # `goals:` is a consequence of the blocked_by fix rather than a decision. It
        # is pinned here because nothing else pins it and `goals:` is the key
        # `resolve_tracked` compares membership on — an edit that moved the unescape
        # out of the helper would otherwise go unnoticed. The other side of that
        # comparison (`declared_members` via `_LIST_ITEM`) reads markdown, not YAML,
        # so it never carries the escape and is deliberately left alone.
        fm = "goals:\n    - '[[Bob''s goal]]'\n"
        self.assertEqual(self.m.fm_wikilinks(fm, "goals"), ["Bob's goal"])

    def test_interior_unmet_entry_is_read(self):
        self.blocker("Met One", "completed")
        self.blocker("Unmet Middle", "next")
        self.blocker("Met Two", "completed")
        fm = (
            "blocked_by:\n"
            "    - '[[Met One]]'\n"
            "    - '[[Unmet Middle]]'\n"
            "    - '[[Met Two]]'\n"
        )
        v = self.verdict(fm)
        self.assertEqual(v["entries"], ["Met One", "Unmet Middle", "Met Two"])
        self.assertEqual(v["unmet"], ["Unmet Middle"])
        self.assertTrue(v["blocked"])

    def test_all_met_is_the_negative_control(self):
        # Positive without negative is a probe that cannot fail: the interior case
        # above passes against a build that answers "blocked" too eagerly.
        for n in ("Met One", "Met Middle", "Met Two"):
            self.blocker(n, "completed")
        fm = (
            "blocked_by:\n"
            "    - '[[Met One]]'\n"
            "    - '[[Met Middle]]'\n"
            "    - '[[Met Two]]'\n"
        )
        v = self.verdict(fm)
        self.assertEqual(v["unmet"], [])
        self.assertFalse(v["blocked"])

    def test_missing_blocker_counts_as_unmet(self):
        # "Cannot verify it is done" reads as blocked, never as permission to start.
        fm = "blocked_by:\n    - '[[Never Written]]'\n"
        v = self.verdict(fm)
        self.assertEqual(v["unmet"], ["Never Written"])
        self.assertTrue(v["blocked"])

    def test_no_blocked_by_is_unblocked(self):
        v = self.verdict("status: next\n")
        self.assertEqual(v["entries"], [])
        self.assertFalse(v["blocked"])


class TestBlockedVerdictsMode(Base):
    """The CLI seam the sweep reader actually calls — `--blocked-verdicts`.

    `TestBlockedByRead` above pins `blocked_by_verdict` in isolation; this pins the
    WIRING, which is the half that was missing: the function landed on 2026-10-03 with
    no non-test caller, so the reader went on deciding `ready-to-start` from its own
    frontmatter read and re-promoted blocked rows the same evening. A tested function
    nothing calls ships nothing, so what is asserted here is the reader's own input —
    one line per staged row, carrying the word the reader is told to trust.

    The interior case is the discriminator, not decoration: with entries `[met, unmet,
    met]` a first-entry-only read answers `ready`, which is exactly the promotion this
    task exists to stop. Every case also names its control, because a build that answers
    `blocked` too eagerly satisfies the positive case alone.
    """

    SUBJECT = "ATopic"

    def stage(self, *names):
        """Write the caller's staged tracked set — the file the mode classifies.

        `--write-tracked` creates the state dir; a test that plants the file directly
        has to do the same, or it asserts against a missing directory rather than
        against the mode.
        """
        os.makedirs(self.m.STATE_DIR, exist_ok=True)
        with open(self.m.tracked_path(self.SUBJECT), "w", encoding="utf-8") as fh:
            fh.write("".join(f"{n}\n" for n in names))

    def row(self, name, blocked_by=None):
        body = "---\npage_type: task\nstatus: next\n"
        if blocked_by is not None:
            body += "blocked_by:\n" + "".join(f"    - '[[{b}]]'\n" for b in blocked_by)
        self.write(f"25 Tasks/{name}.md", body + "---\nTags: [[Task]]\n")

    def blocker(self, name, status):
        self.write(
            f"25 Tasks/{name}.md",
            f"---\npage_type: task\nstatus: {status}\n---\nTags: [[Task]]\n",
        )

    def lines(self):
        rc, out = self.run_gate("--subject", self.SUBJECT, "--blocked-verdicts")
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        return dict(
            (parts[0], parts) for parts in (ln.split("\t") for ln in out.strip().splitlines())
        )

    def test_interior_unmet_entry_reads_blocked(self):
        # [met, UNMET, met] — a first-entry-only read answers `ready` here, which is
        # the promotion measured on 2026-10-02 and again 2026-10-03 18:44.
        self.blocker("Met One", "completed")
        self.blocker("Unmet Middle", "next")
        self.blocker("Met Two", "completed")
        self.row("ARow", ["Met One", "Unmet Middle", "Met Two"])
        self.stage("ARow")
        line = self.lines()["ARow"]
        self.assertEqual(line[1], "blocked")
        self.assertEqual(line[2], "Unmet Middle")

    def test_every_entry_met_reads_ready(self):
        # The negative control: without it, a build that answers `blocked` to
        # everything passes the interior case and blocks the whole vault.
        for n in ("Met One", "Met Middle", "Met Two"):
            self.blocker(n, "completed")
        self.row("ARow", ["Met One", "Met Middle", "Met Two"])
        self.stage("ARow")
        self.assertEqual(self.lines()["ARow"][1], "ready")

    def test_no_blocked_by_reads_ready(self):
        # Vacuously satisfied, and it must stay that way: treating an absent list as
        # blocked would empty the bucket for every blocker-less task in the vault.
        self.row("ARow")
        self.stage("ARow")
        self.assertEqual(self.lines()["ARow"][1], "ready")

    def test_missing_blocker_reads_blocked(self):
        # "Cannot verify it is done" is not permission to start.
        self.row("ARow", ["Never Written"])
        self.stage("ARow")
        self.assertEqual(self.lines()["ARow"][2], "Never Written")

    def test_escaped_apostrophe_does_not_falsely_block(self):
        # The second defect the predecessor found: a regex read returns the
        # still-escaped `Supervisor''s`, which resolves to no page — and "no page"
        # reads as unmet, so a COMPLETED blocker would render blocked-upstream.
        self.blocker("Add a Cluster Spawn Mode to the Supervisor's spawn_agent", "completed")
        self.row("ARow", ["Add a Cluster Spawn Mode to the Supervisor's spawn_agent"])
        self.stage("ARow")
        self.assertEqual(self.lines()["ARow"][1], "ready")

    def test_an_unreadable_row_is_not_reported_ready(self):
        # A row whose own file cannot be read has an EMPTY frontmatter, which
        # `blocked_by_verdict` would call unblocked — the vacuous promotion itself.
        self.stage("Never Written")
        rc, out = self.run_gate("--subject", self.SUBJECT, "--blocked-verdicts")
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        # FOUR columns, matching the ready/blocked branch — the trailing empties are
        # fields, not padding. The count cell is empty rather than `0`: nothing about
        # this row's entries is known, and a consumer reading `0` would conclude the
        # row declares no blocker, which is a different (and wrong) claim.
        self.assertEqual(
            out.rstrip("\n").split("\t"), ["Never Written", "unreadable", "", ""]
        )

    def test_resolve_task_file_returns_the_canonical_spelling(self):
        # APFS is case-insensitive, so `open()` hides an exact-only regression: the
        # assertion is on the resolved PATH, which is the only thing that differs
        # between the two builds on this host. Asserting the verdict instead would
        # pass either way and pin nothing.
        self.blocker("Met Alpha", "completed")
        tasks = os.path.join(self.vault, "25 Tasks")
        self.assertEqual(
            os.path.basename(self.m.resolve_task_file(tasks, "met alpha")),
            "Met Alpha.md",
        )
        self.assertEqual(
            os.path.basename(self.m.resolve_task_file(tasks, "Never Written")),
            "Never Written.md",
        )

    def test_a_case_mismatched_blocker_does_not_block(self):
        # End to end, the same property the resolver test pins one level down.
        self.blocker("Met Alpha", "completed")
        self.row("ARow", ["met alpha"])
        self.stage("ARow")
        self.assertEqual(self.lines()["ARow"][1], "ready")

    def test_a_tab_in_a_name_cannot_shift_the_columns(self):
        # A task name is frontmatter-controlled, and every consumer of this output
        # splits on tabs — including this test class.
        self.blocker("Met", "completed")
        self.row("A\tRow", ["Met"])
        self.stage("A\tRow")
        rc, out = self.run_gate("--subject", self.SUBJECT, "--blocked-verdicts")
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        self.assertEqual(len(out.rstrip("\n").split("\t")), 4)

    def test_the_entry_count_distinguishes_no_list_from_all_met(self):
        # Both render an empty unmet column, so a report rule keyed on that column
        # would skip every all-met row — and an all-met row is where a first-entry-only
        # read and the verdict agree by accident, which is why the rows declaring a
        # blocker are the ones the sweep report has to quote back.
        for n in ("Met One", "Met Two"):
            self.blocker(n, "completed")
        self.row("AWithList", ["Met One", "Met Two"])
        self.row("AWithoutList")
        self.stage("AWithList", "AWithoutList")
        lines = self.lines()
        self.assertEqual(lines["AWithList"][3], "2")
        self.assertEqual(lines["AWithoutList"][3], "0")
        self.assertEqual(lines["AWithList"][2], "")
        self.assertEqual(lines["AWithoutList"][2], "")

    def test_a_bare_name_entry_is_unmet_not_absent(self):
        # `blocked_by` entries are names OR `[[wikilinks]]`. A bare name yields no
        # `[[...]]` match, so reading it as an EMPTY list would satisfy the
        # `ready-to-start` clause vacuously — the one direction this mode must never
        # fail in, and the shape the rule it replaces explicitly admits.
        self.write(
            "25 Tasks/ARow.md",
            "---\npage_type: task\nstatus: next\nblocked_by:\n    - Met One\n"
            "---\nTags: [[Task]]\n",
        )
        self.stage("ARow")
        line = self.lines()["ARow"]
        self.assertEqual(line[1], "blocked")
        self.assertEqual(line[2], self.m.UNPARSED_BLOCKER)

    def test_a_declared_but_empty_list_is_still_ready(self):
        # The other side of the same rule, and the reason it cannot be a blanket
        # "the key is present" test: `blocked_by: []` and a bare `blocked_by:`
        # declare NOTHING, and a blocker-less approved row must still read ready.
        # Over-blocking here would empty the bucket for the whole vault.
        for body in ("blocked_by: []\n", "blocked_by:\n"):
            self.write(
                "25 Tasks/ARow.md",
                "---\npage_type: task\nstatus: next\n" + body + "---\nTags: [[Task]]\n",
            )
            self.stage("ARow")
            self.assertEqual(self.lines()["ARow"][1], "ready", repr(body))

    def test_a_case_mismatched_row_name_still_classifies(self):
        # Both halves of the mode go through the resolver — the row as well as its
        # blockers. Asserted end to end here; the discriminating assertion on the
        # resolved path lives in test_resolve_task_file_returns_the_canonical_spelling.
        self.blocker("Met", "completed")
        self.row("ARow", ["Met"])
        self.stage("arow")
        self.assertEqual(self.lines()["arow"][1], "ready")

    def test_every_staged_row_is_classified_in_order(self):
        self.blocker("Met", "completed")
        self.row("AReady", ["Met"])
        self.row("ABlocked", ["Never Written"])
        self.stage("AReady", "ABlocked")
        rc, out = self.run_gate("--subject", self.SUBJECT, "--blocked-verdicts")
        self.assertEqual(rc, self.m.EXIT_WRITE_OK, out)
        self.assertEqual(
            [ln.split("\t")[0] for ln in out.strip().splitlines()],
            ["AReady", "ABlocked"],
        )

    def test_no_staged_set_is_a_usage_error_not_an_empty_pass(self):
        # "I could not read the set" and "every row is unblocked" must never render
        # the same way — and the second is the one that opens a spawn.
        rc, out = self.run_gate("--subject", self.SUBJECT, "--blocked-verdicts")
        self.assertEqual(rc, self.m.EXIT_USAGE)
        self.assertEqual(out.strip(), "")


class DeclaredMembersBoundary(unittest.TestCase):
    """`## Goals` membership must not be truncated by a `###`.

    Topic pages legitimately carry `###` prose notes and phase sub-headings inside
    `## Goals` — `23 Topics/Unattended Execution.md` splits its membership into
    `### Phase 1` / `### Phase 2`. The section lookahead used to terminate on
    `#{2,3}`, i.e. on a `###` as well as a `##`, so every member below the first
    `###` was invisible to the tracked set: measured 2026-10-06 at **52 of 59** on
    `Manager Layer` and **0 of 4** on `Unattended Execution`.

    ⚠️ Both halves are asserted, and they enforce different things. The first is the
    discriminating one: it asserts the *whole ordered list*, so a capture widened to
    the end of the document returns a third member and fails it. The second states
    the boundary positively and is weaker in isolation — `assertNotIn` would also
    pass against an empty list — but it is what names the invariant a "just read to
    the end" edit would break. (Reviewer's correction, 2026-10-06: an earlier draft
    of this paragraph claimed the first test would pass under such a widening. It
    would not.)
    ⚠️ `reset.py:members_of` carries the same lookahead and has no other coverage.
    """

    PAGE = """---
page_type: topic
---

## Goals

- [[BeforeTheNote]]

### Phase 1 — a prose note or a phase sub-heading

- [[AfterTheNote]]

## Non-goals

- [[NotAMember]]
"""

    def page(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "Topic.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.PAGE)
        return tmp, path

    def test_an_h3_inside_goals_does_not_truncate_the_member_list(self):
        tmp, path = self.page()
        m = load(os.path.join(tmp, "state"))
        self.assertEqual(m.declared_members(path), ["BeforeTheNote", "AfterTheNote"])

    def test_a_later_h2_section_still_terminates_the_section(self):
        tmp, path = self.page()
        m = load(os.path.join(tmp, "state"))
        self.assertNotIn("NotAMember", m.declared_members(path))

    def test_reset_members_of_shares_the_boundary(self):
        # reset.py carries the same lookahead and had no coverage at all; if the two
        # ever diverge, the gate and `reset` disagree about the tracked set silently.
        tmp, path = self.page()
        spec = importlib.util.spec_from_file_location(
            "reset", os.path.join(os.path.dirname(_SCRIPT), "reset.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.members_of(path), ["BeforeTheNote", "AfterTheNote"])


if __name__ == "__main__":
    unittest.main()
