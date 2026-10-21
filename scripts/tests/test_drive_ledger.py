"""drive-ledger.py: the merge `agents/fleet-drive.md` used to hand-write.

The defect this guards, measured 2026-10-12: a jq expression in the agent's step 7
collapsed this ledger from 376 entries to 1. The prose already said "Merge, never
replace" and had said it for months; prose has nothing to run against it, so the merge
moved into a script and this file is what fails it.

Three things here are load-bearing, and each is a way the suite could be plausibly
wrong:

- **The five mutated variants.** A suite that only exercises the reference
  implementation proves the script works today, not that these tests would catch it
  regressing tomorrow. Each variant is the real `drive-ledger.py` with ONE source-level
  break applied, and `TestDiscrimination` requires each to violate **exactly its own
  invariant** -- not "it exits non-zero", which an unrelated crash would satisfy, and
  not "something went wrong", which would not say which assertion caught it.

- **The no-op control.** Every post-merge assertion reads the file the script
  produced. The fixture is deliberately the TWO-SHAPE one, so an unrun merge leaves 5
  `entries` rows and 16 flat sid keys -- failing both the count assertion (18) and the
  shape assertion. Against a canonical fixture an unrun merge would satisfy both, and
  "the count did not fall" would be the whole test.

- **The fixture's overlap.** 3 sids appear in BOTH shapes. Modelling only disjoint
  shapes leaves the read-time precedence rule untested, and the live file stopped
  being disjoint between 13:55 and 14:13 that day. One overlapping flat row carries a
  different `open_boxes` from its `entries` row, so a precedence bug shows up as a
  wrong NUMBER rather than only as a wrong shape.

The fixture models the live file as measured 2026-10-12 14:13, with invented names:
16 flat sid-keyed rows (3 of them also carrying `open_boxes` -- the flat shape is not
internally uniform), 5 `entries`-list rows, 3 sids in both, 18 distinct sessions.
"""
import glob
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "drive-ledger.py"

#: The canonical entry's ten fields, as `agents/fleet-drive.md` step 7 names them.
#: `task_file` is what step 0 re-probes from; without it a retained row cannot be
#: resolved back to a task at all. `reprobe_verdict`/`reprobe_probe` hold step 0's own
#: observation, kept apart from `verdict`/`probe` (the agent's conclusion) because the
#: two vocabularies never coincide -- `needs-step-4` is emitted by the re-probe and
#: deliberately absent from VERDICTS.
FIELDS = {
    "session_id",
    "name",
    "verdict",
    "open_boxes",
    "probe",
    "nudge",
    "suppression",
    "task_file",
    "reprobe_verdict",
    "reprobe_probe",
}

FLAT_ONLY = tuple("flat%05d" % i for i in range(13))
ENTRY_ONLY = ("entry0001", "entry0002")
OVERLAP = ("overlap01", "overlap02", "overlap03")
ALL_FIXTURE_SIDS = set(FLAT_ONLY) | set(ENTRY_ONLY) | set(OVERLAP)

#: The overlapping sids resolve to their `entries` row, which carries this count. The
#: first overlap's FLAT row carries a different one, so a precedence bug is visible as
#: a wrong number rather than only as a wrong shape.
OVERLAP_BOXES = 3
FLAT_DECOY_BOXES = 9
#: A flat-only row's count. Distinct from the two above so a precedence bug cannot
#: coincidentally read as correct.
FLAT_BOXES = 4

CORRUPT_TEXT = "{ this is not json"


def entry(sid, **over):
    row = {
        "session_id": sid,
        "name": "Session " + sid,
        "verdict": "unverifiable",
        "open_boxes": 0,
        "probe": "no CLOSERS row this round; fail closed",
        "nudge": "none",
        "suppression": "none",
    }
    row.update(over)
    return row


def flat(sid, **over):
    """A legacy row: `sid` instead of `session_id`, never `suppression`."""
    row = {
        "sid": sid,
        "name": "Session " + sid,
        "verdict": "unverifiable",
        "probe": "digest rows lack task column",
        "nudge": "none",
    }
    row.update(over)
    return row


def legacy_fixture():
    """16 flat rows + 5 `entries` rows, 3 sids in both, 18 distinct."""
    data = {"round": "2026-10-12T11-55-10Z", "entries": []}
    for i, sid in enumerate(FLAT_ONLY):
        # 2 of the flat-only rows carry `open_boxes`, matching the live file where 3 of
        # 16 flat rows did -- so the third with the field is the overlap decoy below.
        data[sid] = flat(sid, **({"open_boxes": FLAT_BOXES} if i < 2 else {}))
    for i, sid in enumerate(OVERLAP):
        data[sid] = flat(sid, **({"open_boxes": FLAT_DECOY_BOXES} if i == 0 else {}))
    data["entries"] = [entry(sid, open_boxes=OVERLAP_BOXES) for sid in OVERLAP]
    data["entries"] += [entry(sid) for sid in ENTRY_ONLY]
    return data


def new_entries(count=2):
    """This round's payload -- sessions the fixture has never seen."""
    return [
        entry("fresh%04d" % i, verdict="revive", nudge="drafted") for i in range(count)
    ]


def payload(rows, round_stamp="2026-10-12T12-10-00Z"):
    return {"round": round_stamp, "entries": rows}


def read_bytes(path):
    """Bytes as they stand. The `.bak` assertion is byte-identity, not parsed equality."""
    with open(path, "rb") as handle:
        return handle.read()


#: Each variant is the real script with ONE source-level break, so the suite is proven
#: to discriminate against a regression in the implementation rather than against a
#: hand-written stub. Value is (find, replace).
MUTATIONS = {
    # (i) drops every session absent from this round's payload.
    "drops-absent-sessions": (
        "        merged = collect(data)\n        merged.update(fresh)\n",
        "        merged = dict(fresh)\n",
    ),
    # (ii) writes without taking the pre-merge copy.
    "skips-bak": (
        "        had_bak = snapshot()\n",
        "        had_bak = False\n",
    ),
    # (iii) replaces the file in place instead of renaming over it.
    "non-atomic-write": (
        "        os.replace(tmp, LEDGER)\n",
        "        shutil.copyfile(tmp, LEDGER)\n",
    ),
    # (iv) keeps the legacy top-level shape instead of normalising to `entries`.
    "unnormalised-shape": (
        '        write({"round": payload.get("round"), "entries": ordered(merged)})\n',
        '        write(dict(data, **{"round": payload.get("round"),'
        ' "entries": ordered(merged)}))\n',
    ),
    # (v) reads an unparseable ledger as an empty one, then overwrites it.
    "accepts-corrupt-ledger": (
        "    except (OSError, ValueError) as exc:\n        raise Corrupt(str(exc))\n",
        "    except (OSError, ValueError) as exc:\n        return {}, False\n",
    ),
}

#: The one invariant each variant is required to violate -- and, per
#: TestDiscrimination, the ONLY one it may violate.
EXPECTED_VIOLATION = {
    "drops-absent-sessions": "retains-absent-sessions",
    "skips-bak": "takes-a-bak",
    "non-atomic-write": "consumes-the-tmp",
    "unnormalised-shape": "writes-one-shape",
    "accepts-corrupt-ledger": "refuses-a-corrupt-ledger",
}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="drive-ledger-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ledger = os.path.join(self.tmp, "ledger.json")

    def env(self):
        return dict(os.environ, SUPERVISOR_DRIVE_LEDGER=self.ledger)

    def run_script(self, args, stdin="", script=None):
        return subprocess.run(
            [sys.executable, str(script or SCRIPT)] + args,
            input=stdin,
            capture_output=True,
            text=True,
            env=self.env(),
        )

    def seed(self, data=None):
        data = legacy_fixture() if data is None else data
        with open(self.ledger, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
        return data

    def on_disk(self):
        with open(self.ledger, encoding="utf-8") as handle:
            return json.load(handle)

    def rows_on_disk(self):
        return {row["session_id"]: row for row in self.on_disk().get("entries", [])}

    def sids_on_disk(self):
        return set(self.rows_on_disk())

    def merge(self, rows, **kw):
        return self.run_script(["merge"], json.dumps(payload(rows, **kw)))

    def mutate(self, name):
        """Write the real script with one break applied, and return its path."""
        find, replace = MUTATIONS[name]
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            find,
            source,
            "variant %r no longer applies -- its anchor moved; re-point the mutation "
            "rather than deleting the variant" % name,
        )
        path = os.path.join(self.tmp, "drive-ledger-%s.py" % name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(source.replace(find, replace, 1))
        return path


class TestRead(Base):
    def test_normalises_both_shapes_to_one(self):
        self.seed()
        proc = self.run_script(["read"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)

        self.assertEqual(set(out), {"round", "entries"})
        self.assertEqual(len(out["entries"]), 18, "16 flat + 5 rows - 3 overlapping")
        self.assertEqual(
            len({row["session_id"] for row in out["entries"]}),
            18,
            "exactly one entry per session id",
        )
        for row in out["entries"]:
            self.assertEqual(set(row), FIELDS, "canonical shape, all ten fields")

    def test_absent_ledger_reads_empty(self):
        proc = self.run_script(["read"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {"round": None, "entries": []})

    def test_refuses_an_unparseable_ledger(self):
        """A false "empty" is the 2026-09-23 regression: 37 entries read as 8."""
        with open(self.ledger, "w", encoding="utf-8") as handle:
            handle.write(CORRUPT_TEXT)
        proc = self.run_script(["read"])
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertNotIn("entries", proc.stdout, "an empty read must not be printed")
        self.assertIn("cannot be parsed", proc.stderr)


class TestMerge(Base):
    def test_keeps_every_session_absent_this_round(self):
        self.seed()
        proc = self.merge(new_entries(2))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self.sids_on_disk(), ALL_FIXTURE_SIDS | {"fresh0000", "fresh0001"}
        )
        self.assertEqual(len(self.sids_on_disk()), 20)

    def test_entries_row_wins_over_the_flat_row(self):
        """Precedence case 3: the canonical row is the newer write."""
        self.seed()
        self.merge([])
        rows = self.rows_on_disk()
        self.assertEqual(rows["overlap01"]["open_boxes"], OVERLAP_BOXES)
        self.assertNotEqual(rows["overlap01"]["open_boxes"], FLAT_DECOY_BOXES)

    def test_legacy_row_without_open_boxes_stays_null_not_zero(self):
        """0 means verified-finished; null means never counted. Never conflate them."""
        self.seed()
        self.merge([])
        rows = self.rows_on_disk()
        self.assertIsNone(rows[FLAT_ONLY[5]]["open_boxes"])
        self.assertEqual(rows[FLAT_ONLY[5]]["suppression"], "none")

    def test_legacy_row_carrying_open_boxes_keeps_it(self):
        """3 of the 16 live flat rows already had the field; normalisation must not eat it."""
        self.seed()
        self.merge([])
        self.assertEqual(self.rows_on_disk()[FLAT_ONLY[0]]["open_boxes"], FLAT_BOXES)

    def test_writes_the_bak_byte_identical_to_the_pre_merge_file(self):
        self.seed()
        with open(self.ledger, "rb") as handle:
            before = handle.read()
        self.merge(new_entries(1))

        bak = self.ledger + ".bak"
        self.assertTrue(
            os.path.exists(bak), "a merge over an existing ledger takes a .bak"
        )
        with open(bak, "rb") as handle:
            self.assertEqual(handle.read(), before, "the .bak is the file as it stood")

    def test_first_merge_has_nothing_to_back_up(self):
        proc = self.merge(new_entries(1))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(os.path.exists(self.ledger + ".bak"))
        self.assertIn("bak: no", proc.stdout)

    def test_the_tmp_file_does_not_survive_the_rename(self):
        self.seed()
        self.merge(new_entries(1))
        self.assertEqual(
            glob.glob(self.ledger + ".tmp.*"), [], "the rename consumed it"
        )

    def test_reports_the_entry_count_and_round(self):
        self.seed()
        proc = self.merge(new_entries(2), round_stamp="2026-10-12T12-10-00Z")
        self.assertIn("ledger written: 20 entries", proc.stdout)
        self.assertIn("2026-10-12T12-10-00Z", proc.stdout)
        self.assertIn("bak: yes", proc.stdout)


class TestMergeRefusals(Base):
    def assert_refused(self, proc, needle):
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn(needle, proc.stderr)
        self.assertEqual(proc.stdout, "", "a refused merge prints no success line")

    def test_refuses_an_unparseable_ledger_and_leaves_it_alone(self):
        with open(self.ledger, "w", encoding="utf-8") as handle:
            handle.write(CORRUPT_TEXT)
        proc = self.merge(new_entries(1))
        self.assert_refused(proc, "cannot be parsed")
        with open(self.ledger, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), CORRUPT_TEXT, "never overwritten")

    def test_refuses_a_payload_that_is_not_json(self):
        self.seed()
        self.assert_refused(self.run_script(["merge"], "not json at all"), "not JSON")

    def test_refuses_a_payload_with_no_entries_list(self):
        self.seed()
        proc = self.run_script(["merge"], json.dumps({"round": "x"}))
        self.assert_refused(proc, "no `entries` list")

    def test_refuses_an_entry_with_no_session_id(self):
        self.seed()
        self.assert_refused(
            self.merge([{"name": "nameless", "verdict": "revive"}]), "no session_id"
        )

    def test_refuses_an_unknown_verdict(self):
        self.seed()
        proc = self.merge([entry("fresh0001", verdict="probably-fine")])
        self.assert_refused(proc, "verdict")

    def test_refuses_an_unknown_nudge(self):
        self.seed()
        self.assert_refused(self.merge([entry("fresh0001", nudge="maybe")]), "nudge")

    def test_refuses_a_session_twice_in_one_payload(self):
        self.seed()
        proc = self.merge([entry("fresh0001"), entry("fresh0001")])
        self.assert_refused(proc, "twice")

    def test_a_refused_merge_writes_nothing_at_all(self):
        self.seed()
        with open(self.ledger, "rb") as handle:
            before = handle.read()
        self.merge([entry("fresh0001", verdict="nope")])
        with open(self.ledger, "rb") as handle:
            self.assertEqual(handle.read(), before)
        self.assertFalse(os.path.exists(self.ledger + ".bak"))


class TestTaskFile(Base):
    """Step 0 of `agents/fleet-drive.md` re-probes a retained row from its task file.

    The path is the only identity field in an entry that can resolve a task: the
    session id is an 8-char display handle, and the name is a session name that need
    not equal the filename. So an entry that loses this field cannot be re-probed at
    all, and it must survive both a read of a legacy row and a merge round-trip.
    """

    def test_a_legacy_row_without_task_file_reads_empty_not_absent(self):
        self.seed()
        proc = self.run_script(["read"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = json.loads(proc.stdout)["entries"]
        self.assertTrue(rows, "fixture must not be empty")
        for row in rows:
            self.assertIn("task_file", row)
            self.assertEqual(row["task_file"], "")

    def test_task_file_survives_a_merge_round_trip(self):
        self.seed()
        path = "/vault/25 Tasks/A Task.md"
        proc = self.merge(
            [{"session_id": "abc12345", "verdict": "unverifiable", "task_file": path}]
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.rows_on_disk()["abc12345"]["task_file"], path)


class TestReprobeKey(Base):
    """Step 0's dedup key, stored separately from the agent's own verdict.

    `drive-retained.py` compares its observation against `reprobe_verdict`/
    `reprobe_probe`. If those fields did not survive the merge, every retained row
    would read as a first observation and re-signal on every round -- the defect
    measured 2026-10-21 as byte-identical PARKED blocks in consecutive rounds.
    """

    def test_legacy_rows_read_the_fields_empty_not_absent(self):
        self.seed()
        proc = self.run_script(["read"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for row in json.loads(proc.stdout)["entries"]:
            self.assertIn("reprobe_verdict", row)
            self.assertEqual(row["reprobe_verdict"], "")
            self.assertIn("reprobe_probe", row)
            self.assertEqual(row["reprobe_probe"], "")

    def test_the_key_survives_a_merge_round_trip(self):
        self.seed()
        proc = self.merge(
            [
                {
                    "session_id": "abc12345",
                    "verdict": "unverifiable",
                    "reprobe_verdict": "needs-step-4",
                    "reprobe_probe": "status=in_progress open_boxes=8, session live",
                }
            ]
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = self.rows_on_disk()["abc12345"]
        self.assertEqual(row["reprobe_verdict"], "needs-step-4")
        self.assertEqual(
            row["reprobe_probe"], "status=in_progress open_boxes=8, session live"
        )


class TestNoOpControl(Base):
    """The assertions above must be able to fail. This pins that they can."""

    def test_an_unrun_merge_fails_both_the_count_and_the_shape_assertion(self):
        self.seed()
        data = self.on_disk()
        top_level_sids = [k for k in data if k not in ("round", "entries")]

        self.assertEqual(len(top_level_sids), 16, "the fixture really is two-shaped")
        self.assertEqual(
            len(data.get("entries", [])),
            5,
            "and its `entries` list holds only 5 -- so a merge that never ran reports 5, "
            "not 18, and the count assertion discriminates rather than passing on a no-op",
        )


class TestDiscrimination(Base):
    """Exit 0 on the reference script; each variant fails its OWN named assertion."""

    def violations(self, script=None, corrupt=False):
        """Which invariants one merge violates. Empty on the reference script.

        Deliberately returns a SET rather than a boolean: SC1 requires each variant to
        fail on its own named assertion, and a boolean cannot distinguish that from a
        variant that fails everything.

        ⚠️ Each call gets a FRESH directory. `test_each_variant_violates_exactly_its_own
        _invariant` runs this in a `subTest` loop, and the variants deliberately leave
        debris -- `non-atomic-write` strands a tmp file, `skips-bak` takes none. Sharing
        one directory let that debris decide the NEXT variant's verdict, so
        `unnormalised-shape` was reported as also violating `consumes-the-tmp` (a
        leftover from the variant before it). The isolation is the test, not tidiness.
        """
        bad = set()
        self.ledger = os.path.join(tempfile.mkdtemp(dir=self.tmp), "ledger.json")

        if corrupt:
            with open(self.ledger, "w", encoding="utf-8") as handle:
                handle.write(CORRUPT_TEXT)
            with open(self.ledger, "rb") as handle:
                before = handle.read()
            self.run_script(
                ["merge"], json.dumps(payload(new_entries(1))), script=script
            )
            with open(self.ledger, "rb") as handle:
                if handle.read() != before:
                    bad.add("refuses-a-corrupt-ledger")
            return bad

        self.seed()
        with open(self.ledger, "rb") as handle:
            before_bytes = handle.read()
        proc = self.run_script(["merge"], json.dumps(payload(new_entries(1))), script=script)
        if proc.returncode != 0:
            return {"crashed: %s" % proc.stderr.strip()}

        data = self.on_disk()
        after = {row["session_id"] for row in data.get("entries", [])}
        if ALL_FIXTURE_SIDS - after:
            bad.add("retains-absent-sessions")
        bak = self.ledger + ".bak"
        if not os.path.exists(bak) or read_bytes(bak) != before_bytes:
            bad.add("takes-a-bak")
        if glob.glob(self.ledger + ".tmp.*"):
            bad.add("consumes-the-tmp")
        if [k for k in data if k not in ("round", "entries")]:
            bad.add("writes-one-shape")
        return bad

    def test_reference_implementation_violates_nothing(self):
        self.assertEqual(self.violations(), set())
        self.assertEqual(self.violations(corrupt=True), set())

    def test_each_variant_violates_exactly_its_own_invariant(self):
        for name, expected in EXPECTED_VIOLATION.items():
            with self.subTest(variant=name):
                self.assertEqual(
                    self.violations(
                        script=self.mutate(name),
                        corrupt=(expected == "refuses-a-corrupt-ledger"),
                    ),
                    {expected},
                    "variant %r must fail on %r and on nothing else" % (name, expected),
                )

    def test_every_variant_anchor_still_applies(self):
        """A moved anchor must fail loudly, not silently stop testing anything."""
        source = SCRIPT.read_text(encoding="utf-8")
        for name, (find, _replace) in MUTATIONS.items():
            with self.subTest(variant=name):
                self.assertEqual(
                    source.count(find),
                    1,
                    "variant %r must match the script exactly once" % name,
                )


if __name__ == "__main__":
    unittest.main()
