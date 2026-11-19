"""park-nudge.py: the enforcement point for the 15-minute park rule.

The defect this guards is a rule that is loaded and ignored. A session sat
`watching` for 31 minutes after the rule landed while a 12-minute one beside it
was legal, and no fleet leg consumed the reading that tells them apart. So the
fixtures pin *discrimination*, not parsing:

- **The threshold discriminates.** Over 15 min emits, inside it does not. A run
  that emits for both, or for neither, fails.
- **The duration is the gate, not the bucket.** A clock/defer park reads `idle`
  (no background shell) while its `🟡 WAITING` episode is still open, so an
  `idle` row over the threshold MUST emit. The bucket fixture pins both sides:
  `watching` and `idle` emit; `gated` (an operator wait) and `working` do not.
- **The duration is `watching_min`, not `quiet_min`.** A watcher re-arm writes to
  the transcript and resets the quiet clock, so a session parked 3 h 34 m read
  `quiet 12m` and was never nudged. The `divergent` fixture carries both fields
  disagreeing; the selection must follow `watching_min`.
- **Suppression is per episode, not per duration value.** `watching_min` grows
  every round, so a key carrying it never suppresses in a live fleet. Run 2
  advances the duration on purpose; run 4 proves re-entry into `watching` re-arms.
- **The nudge carries the park.** register, `defer_date` at a TIME, self-exit
  by a path that resolves, because the self-exit script ships in vault-cli.
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "park-nudge.py"
FIXTURE = json.loads((HERE / "fixtures" / "park-nudge-activity.json").read_text())
OVER = "aaaaaaaa-0000-4000-8000-000000000031"
WITHIN = "bbbbbbbb-0000-4000-8000-000000000012"
# A manager session and the subject its name resolves to. The name is the one the
# goal manager for that subject carries — `<subject> Manager`, the shape
# `/supervisor:open` writes.
MANAGER = "dddddddd-0000-4000-8000-000000000777"
MANAGER_SUBJECT = "No Session Lingers Without Working"
MANAGER_NAME = MANAGER_SUBJECT + " Manager"
MANAGER_BASE = "no-session-lingers-without-working"


def reading(sessions):
    return dict(window_min=15, live=len(sessions), sessions=sessions)


# A session no task claims. Every other sid the fixtures use is anchored by
# `_NudgeBase.setUp`, so this one is the only unanchored row unless a test says otherwise.
UNANCHORED = "99999999-0000-4000-8000-000000000001"


def all_fixture_sids():
    sids = {OVER, WITHIN}
    for block in FIXTURE.values():
        if isinstance(block, dict):
            sids.update(s["sid"] for s in block.get("sessions", []))
    return sids


def row(sid=OVER, bucket="watching", quiet=31, watching=None, since=None,
        name="Synthetic", status=None):
    # The registry status the producer pairs with each bucket by construction:
    # `watching` requires a running shell, `gated` a waiting registry; an `idle`
    # row defaults to an idle registry, and a test that wants the pusher shape
    # (`idle` bucket, `busy` registry) says so.
    if status is None:
        status = {"watching": "shell", "idle": "idle", "gated": "waiting"}.get(bucket, "busy")
    return dict(sid=sid, name=name, status=status, bucket=bucket,
                calls=0, out_tok=0, quiet_min=quiet,
                watching_min=quiet if watching is None else watching,
                watching_since=since,
                waits_on="CI run 1111 completes")


class _NudgeBase(unittest.TestCase):
    """Fixture plumbing shared by both halves; holds no tests of its own."""
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)
        self.state = self.tmp / "state" / "ledger.json"
        # The manager-tier state dir, isolated per test: the gate reads
        # `MANAGER_LIVENESS_STATE_DIR`, and a test that inherited the live one would
        # pass or fail on whatever managers happened to be armed on this host.
        self.mgr_state = self.tmp / "sweep-gate"
        self.mgr_state.mkdir(parents=True)
        # The anchor lookup, isolated per test: `close-worker.py::find_task_checked`
        # reads `SUPERVISOR_VAULTS_ROOT` and `SUPERVISOR_VAULT_CONFIG`, so a test that
        # inherited the live ones would pass or fail on whatever tasks this host has.
        self.vaults = self.tmp / "vaults"
        self.tasks = self.vaults / "v" / "25 Tasks"
        self.tasks.mkdir(parents=True)
        self.vault_config = self.tmp / "vault-config.json"
        self.vault_config.write_text("[]")
        for sid in all_fixture_sids():
            self.anchor(sid)

    def anchor(self, sid, title=None):
        """A task page claiming `sid` through `claude_session_id`."""
        title = title or f"Task {sid[:8]}"
        (self.tasks / f"{title}.md").write_text(
            f"---\nclaude_session_id: {sid}\nstatus: in_progress\n---\n")
        return title

    def run_script(self, data, *args, extra_env=None):
        env = dict(os.environ)
        env.pop("PARK_NUDGE_STATE", None)
        env["MANAGER_LIVENESS_STATE_DIR"] = str(self.mgr_state)
        env["SUPERVISOR_VAULTS_ROOT"] = str(self.vaults)
        env["SUPERVISOR_VAULT_CONFIG"] = str(self.vault_config)
        env.update(extra_env or {})
        payload = data if isinstance(data, str) else json.dumps(data)
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--state", str(self.state), *args],
            input=payload, capture_output=True, text=True, env=env, check=False,
        )

    def arm_manager(self, slug, interval=900, age_s=0):
        """A manager state record: a cadence file `age_s` seconds old."""
        p = self.mgr_state / f"{slug}.cadence"
        p.write_text(f"{interval}\n")
        if age_s:
            at = time.time() - age_s
            os.utime(p, (at, at))
        return p

    @staticmethod
    def nudges(proc):
        return [ln for ln in proc.stdout.splitlines() if ln.startswith("NUDGE ")]


class ParkNudgeTest(_NudgeBase):
    # --- SC1: the threshold discriminates -----------------------------------

    def test_over_threshold_emits_and_within_allowance_does_not(self):
        p = self.run_script(FIXTURE["threshold"])
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = self.nudges(p)
        self.assertEqual(len(lines), 1, p.stdout)
        self.assertIn(OVER, lines[0])
        self.assertIn("quiet 31m", lines[0])
        self.assertNotIn(WITHIN, p.stdout)
        self.assertIn("park-nudge: 1 nudged · 0 suppressed · 1 within 15m · 2 watching · 2 live", p.stdout)

    def test_a_re_arming_watcher_is_nudged_on_its_state_duration(self):
        """The defect: quiet 12m, state 214m. The re-arm must not hide the park."""
        p = self.run_script(FIXTURE["divergent"])
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = self.nudges(p)
        self.assertEqual(len(lines), 1, p.stdout)
        self.assertIn("quiet 214m", lines[0],
                      "the emitted number must be watching_min, not quiet_min")

    def test_a_row_without_a_state_duration_is_skipped_not_treated_as_zero(self):
        """An unmeasured row is not a legal short wait — it is not a candidate,
        but it IS a watching row and must still be counted."""
        unmeasured = row(quiet=99)
        unmeasured.pop("watching_min")
        p = self.run_script(reading([unmeasured]))
        self.assertEqual(self.nudges(p), [])
        self.assertIn("1 watching", p.stdout)

    def test_a_new_episode_re_nudges_even_when_the_duration_grew(self):
        """SC4's missed boundary: two parks with no reading in between.

        Keyed on the duration alone, the second park is suppressed for its whole
        length because the value never fell."""
        self.assertEqual(len(self.nudges(self.run_script(
            reading([row(quiet=16, since="2026-11-08T00:00:00Z")])))), 1)
        self.assertEqual(len(self.nudges(self.run_script(
            reading([row(quiet=40, since="2026-11-08T00:30:00Z")])))), 1,
            "a different episode timestamp must re-arm suppression")

    def test_an_old_transcript_does_not_nudge_a_young_watch(self):
        """SC2: quiet 99m but watching 12m — a stale transcript alone never nudges."""
        p = self.run_script(FIXTURE["stale_transcript"])
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = self.nudges(p)
        self.assertEqual(len(lines), 1, p.stdout)
        self.assertIn("quiet 214m", lines[0])
        self.assertNotIn("bbbbbbbb-0000-4000-8000-000000000013", p.stdout)
        self.assertIn("1 within 15m", p.stdout)

    def test_exactly_fifteen_is_inside_the_allowance(self):
        p = self.run_script(reading([row(quiet=15)]))
        self.assertEqual(self.nudges(p), [], p.stdout)
        p = self.run_script(reading([row(quiet=16)]))
        self.assertEqual(len(self.nudges(p)), 1, p.stdout)

    # --- SC2: the nudge carries the park ------------------------------------

    def test_nudge_text_carries_register_defer_time_and_self_exit_path(self):
        fake = self.tmp / "vcs"
        fake.mkdir()
        (fake / "session-self-exit.py").write_text("")
        home = self.tmp / "home"
        p = self.run_script(FIXTURE["threshold"],
                            extra_env={"VAULT_CLI_SCRIPTS": str(fake), "HOME": str(home)})
        msg = self.nudges(p)[0].split(" :: ", 1)[1]
        park_event = home / ".claude/plugins/marketplaces/claude-supervisor/scripts/park-event.py"
        self.assertIn(f"{park_event} register --task 'Task {OVER[:8]}'", msg)
        self.assertIn("--recheck", msg)
        self.assertIn("`defer_date`", msg)
        self.assertIn("ISO-8601 with offset", msg)
        self.assertIn("Never a bare date", msg)
        self.assertIn(f"python3 {fake / 'session-self-exit.py'}", msg)

    def test_self_exit_falls_back_to_vault_cli_not_this_plugin(self):
        """The self-exit script ships in vault-cli, so the fallback names vault-cli's
        marketplace clone — never this plugin's own scripts dir."""
        home = self.tmp / "home"
        env = {"HOME": str(home), "VAULT_CLI_SCRIPTS": ""}
        msg = self.nudges(self.run_script(FIXTURE["threshold"], extra_env=env))[0]
        self.assertIn(
            f"python3 {home}/.claude/plugins/marketplaces/vault-cli/scripts/session-self-exit.py", msg)
        self.assertNotIn(str(SCRIPT.parent / "session-self-exit.py"), msg)

    def test_handover_paths_carry_no_version_segment(self):
        """A handed-over path pinned to a cache version is the SENDER's
        load, and goes stale for every recipient on another version. Two cache
        versions are installed so a newest-installed resolver has something to pick;
        the emitted paths must be the unversioned marketplace clones, exactly —
        string equality, so a bare or arbitrary unversioned path fails."""
        home = self.tmp / "home"
        for plugin in ("claude-supervisor/supervisor", "vault-cli/vault-cli"):
            for ver in ("0.348.0", "0.348.2"):
                d = home / ".claude/plugins/cache" / plugin / ver / "scripts"
                d.mkdir(parents=True)
                (d / "park-event.py").write_text("")
                (d / "session-self-exit.py").write_text("")
        # The marketplace clones are created too, so the test asserts the handed-over
        # path RESOLVES for the recipient — a path string alone proves nothing about
        # whether the recipient can run it.
        for clone, script in (("claude-supervisor", "park-event.py"),
                              ("vault-cli", "session-self-exit.py")):
            d = home / ".claude/plugins/marketplaces" / clone / "scripts"
            d.mkdir(parents=True, exist_ok=True)
            (d / script).write_text("")
        env = {"HOME": str(home), "VAULT_CLI_SCRIPTS": ""}
        msg = self.nudges(self.run_script(FIXTURE["threshold"], extra_env=env))[0].split(" :: ", 1)[1]
        park_event = f"{home}/.claude/plugins/marketplaces/claude-supervisor/scripts/park-event.py"
        self_exit = f"{home}/.claude/plugins/marketplaces/vault-cli/scripts/session-self-exit.py"
        self.assertTrue(pathlib.Path(park_event).is_file(), "the park-event path must resolve")
        self.assertTrue(pathlib.Path(self_exit).is_file(), "the self-exit path must resolve")
        self.assertIn(f"`python3 {park_event} register", msg)
        self.assertIn(f"`python3 {self_exit}`", msg)
        handed = re.findall(r"python3 (\S+\.py)", msg)
        self.assertEqual(sorted(handed), sorted([park_event, self_exit]), msg)
        for path in handed:
            self.assertIsNone(re.search(r"/\d+\.\d+\.\d+/", path), path)

    # --- SC3: the duration is the gate, not the bucket ----------------------

    def test_watching_and_idle_emit_but_gated_and_working_do_not(self):
        """Every row here is over the threshold. `watching` and `idle` are the
        union population; `gated` waits on the operator and `working` has no
        open episode, so neither may emit."""
        watched = {r["bucket"]: r["watching_min"] for r in FIXTURE["buckets"]["sessions"]}
        self.assertEqual(watched, {"working": 40, "gated": 22, "idle": 60, "watching": 31})
        p = self.run_script(FIXTURE["buckets"])
        lines = self.nudges(p)
        self.assertEqual(len(lines), 2, p.stdout)
        self.assertIn(OVER, "".join(lines), "the watching row must emit")
        self.assertIn("eeeeeeee-0000-4000-8000-000000000060", "".join(lines),
                      "the idle (clock/defer) row must emit")
        self.assertNotIn("cccccccc", p.stdout, "a working row has no open episode")
        self.assertNotIn("dddddddd", p.stdout, "a gated row waits on the operator")

    # --- SC4: one nudge per watching episode --------------------------------

    def test_suppression_survives_growing_duration_and_rearms_on_reentry(self):
        runs = [("watching", 31, 1), ("watching", 35, 0), ("gated", 35, 0), ("watching", 35, 1)]
        for i, (bucket, quiet, want) in enumerate(runs, 1):
            p = self.run_script(reading([row(bucket=bucket, quiet=quiet)]))
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(len(self.nudges(p)), want, f"run {i} ({bucket} {quiet}m): {p.stdout}")

    def test_a_duration_dropping_starts_a_new_episode(self):
        self.assertEqual(len(self.nudges(self.run_script(reading([row(quiet=31)])))), 1)
        # A fresh measurement (e.g. after a transcript rotation): nudge again.
        self.assertEqual(len(self.nudges(self.run_script(reading([row(quiet=20)])))), 1)

    def test_absence_from_the_reading_ends_the_episode(self):
        self.run_script(reading([row(quiet=31)]))
        self.run_script(reading([]))
        self.assertEqual(len(self.nudges(self.run_script(reading([row(quiet=40)])))), 1)

    def test_dry_run_never_writes_the_ledger(self):
        self.run_script(reading([row(quiet=31)]), "--dry-run")
        self.assertFalse(self.state.exists())
        self.assertEqual(len(self.nudges(self.run_script(reading([row(quiet=31)])))), 1)

    # --- an unread fleet is never a clean one -------------------------------

    def test_unreadable_reading_is_unknown_not_zero(self):
        for bad in ("", "not json", json.dumps({"counts": {}})):
            p = self.run_script(bad)
            self.assertEqual(p.returncode, 2, bad)
            self.assertTrue(p.stdout.startswith("park-nudge: unknown"), p.stdout)
            self.assertEqual(self.nudges(p), [])

    def test_corrupt_ledger_is_unknown(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text("[]")
        p = self.run_script(reading([row(quiet=31)]))
        self.assertEqual(p.returncode, 2)
        self.assertIn("ledger unreadable", p.stdout)

    def test_threshold_is_read_from_park_event(self):
        src = SCRIPT.read_text()
        self.assertIn("PARK_THRESHOLD_SECONDS", src)
        self.assertNotIn("15 * 60", src)


class ManagerTierTest(_NudgeBase):
    """A manager-tier session is never drafted for a park nudge.

    The defect is structural, not a threshold or a `--state` reading: the park
    mechanism binds an event key to an ANCHORED TASK, a manager has none, and the
    exit the nudge asks for closes the pane that is a goal manager's only wake
    path. `OK` is the normal state of a healthy armed manager, so the guard has to
    be a TIER test — measured 2026-11-12, four managers drafted in one round at
    28%, 52%, 55% and 0% of their own bounds, every one healthy.

    The pre-fix control is the whole of `ParkNudgeTest`: it drafts for every row it
    is given, so a zero from the cases below is discriminating rather than a round
    with nothing to draft.
    """

    def manager_row(self, sid=MANAGER, name=MANAGER_NAME, quiet=31):
        return row(sid=sid, name=name, quiet=quiet)

    def session_marker(self, slug, sid):
        (self.mgr_state / f"{slug}.session").write_text(f"{sid}\n")

    # --- SC1: the tier test ------------------------------------------------

    def test_a_session_that_armed_a_manager_cadence_is_never_nudged(self):
        """Signal 1 — the only POSITIVE tier statement on disk: `--arm` runs inside
        the manager's own session, so the id it records is the manager's."""
        self.arm_manager(f"goal--{MANAGER_BASE}")
        self.session_marker(f"goal--{MANAGER_BASE}", MANAGER)
        p = self.run_script(reading([self.manager_row()]))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.nudges(p), [], p.stdout)
        self.assertIn("0 nudged", p.stdout)
        self.assertIn("1 manager-skipped", p.stdout)

    def test_a_manager_is_never_nudged_on_its_name_alone(self):
        """Signal 2 — covers a manager that armed before the marker existed. Its
        next arm writes one; until then the name is the evidence."""
        self.arm_manager(f"goal--{MANAGER_BASE}")
        p = self.run_script(reading([self.manager_row()]))
        self.assertEqual(self.nudges(p), [], p.stdout)
        self.assertIn("1 manager-skipped", p.stdout)

    def test_a_stale_manager_is_still_never_nudged(self):
        """A STALE manager is RESTARTED by `topic-keepalive.py`, never parked: the
        park would close the pane the restart needs to reach. So the gate keys on
        tier, not on `--state` reading OK."""
        self.arm_manager(f"topic--{MANAGER_BASE}", age_s=10_000)
        p = self.run_script(reading([self.manager_row()]))
        self.assertEqual(self.nudges(p), [], p.stdout)
        self.assertIn("1 manager-skipped", p.stdout)

    # --- SC2: the `--state` backstop ---------------------------------------

    def test_an_ok_state_inside_its_bound_blocks_a_draft(self):
        """The backstop, quoted: an over-threshold subject reading `OK age=0s
        limit=1080s` (n < m) drafts nothing. This is the exact shape the round-44
        control failed on."""
        p = self.arm_manager(f"goal--{MANAGER_BASE}")
        self.assertEqual(int(time.time() - p.stat().st_mtime), 0,
                         "the record must be fresh, so --state reads OK inside its bound")
        out = self.run_script(reading([self.manager_row()]))
        self.assertEqual(self.nudges(out), [], out.stdout)
        self.assertIn("1 manager-skipped", out.stdout)

    def test_a_legacy_cadence_blocks_a_draft_through_the_state_guard(self):
        """A pre-split record (`<base>.cadence`, no branch prefix) is not in the
        slug set the name test matches, so only the `--state` read catches it —
        which is why the backstop exists rather than being a restatement of the
        tier test."""
        self.arm_manager(MANAGER_BASE)
        p = self.run_script(reading([self.manager_row()]))
        self.assertEqual(self.nudges(p), [], p.stdout)
        self.assertIn("1 manager-skipped", p.stdout)

    # --- SC3: worker nudging is unchanged ----------------------------------

    def test_a_worker_past_its_bound_is_still_nudged(self):
        self.arm_manager(f"goal--{MANAGER_BASE}")
        self.session_marker(f"goal--{MANAGER_BASE}", MANAGER)
        p = self.run_script(reading([
            self.manager_row(),
            row(sid=OVER, name="Fix the park rule", quiet=31),
        ]))
        lines = self.nudges(p)
        self.assertEqual(len(lines), 1, p.stdout)
        self.assertIn(OVER, lines[0])
        self.assertNotIn(MANAGER, p.stdout)
        self.assertIn("1 manager-skipped", p.stdout)

    def test_a_worker_whose_name_is_not_a_manager_subject_is_unaffected(self):
        """No manager state at all: the gate must not suppress anything."""
        p = self.run_script(reading([row(sid=OVER, name="Fix the park rule", quiet=31)]))
        self.assertEqual(len(self.nudges(p)), 1, p.stdout)
        self.assertIn("0 manager-skipped", p.stdout)

    # --- the gate is observable -------------------------------------------

    def test_the_json_names_why_each_manager_was_skipped(self):
        self.arm_manager(f"goal--{MANAGER_BASE}")
        self.session_marker(f"goal--{MANAGER_BASE}", MANAGER)
        p = self.run_script(reading([self.manager_row()]), "--json")
        data = json.loads(p.stdout)
        self.assertEqual(data["manager"], 1)
        self.assertEqual(data["manager_rows"][0]["sid"], MANAGER)
        self.assertIn("armed as goal--", data["manager_rows"][0]["why"])


BUSY = "77777777-0000-4000-8000-000000000029"
IDLE = "88888888-0000-4000-8000-000000000029"


class RegistryStatusTest(_NudgeBase):
    """An `idle` bucket is not a parked session — the registry `status` says which.

    `session-activity.py::bucket_for` lets a `status: busy` row with no tool calls in
    its window fall through to `idle`. A pusher is exactly that between rounds, and it
    received six `PARK NOW` orders while its registry read `busy`. Its `watching_min`
    then went 29 → 17 between two sends, because it restarts per `🟡 WAITING` episode.
    A role exclusion would only move the bug onto the next role that does not fit; the
    registry status is the reading that says whether a session is actually idle.
    """

    def setUp(self):
        super().setUp()
        self.anchor(BUSY)
        self.anchor(IDLE)

    def test_an_idle_bucket_nudges_only_when_the_registry_reads_idle(self):
        """Both rows are pinned to `bucket: idle`, both past the threshold. Only
        the registry status differs, so only the status read can separate them."""
        p = self.run_script(reading([
            row(sid=BUSY, bucket="idle", status="busy", quiet=29),
            row(sid=IDLE, bucket="idle", status="idle", quiet=29),
        ]))
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = self.nudges(p)
        self.assertEqual(len(lines), 1, p.stdout)
        self.assertIn(IDLE, lines[0])
        self.assertNotIn(BUSY, "".join(lines))

    def test_the_pusher_shape_is_never_ordered_to_park(self):
        """The measured shape — `idle` bucket, `busy` registry, `watching_min` 29,
        an open episode — beside a genuinely idle row of the same duration. The busy
        row is absent from the PARK NOW set while the idle one is present, and the
        skip is counted so a silent round can be told from a round that skipped."""
        since = "2026-11-18T19:40:00Z"
        p = self.run_script(reading([
            row(sid=BUSY, name="Fleet Pusher", bucket="idle", status="busy",
                quiet=29, since=since),
            row(sid=IDLE, name="Parked Worker", bucket="idle", status="idle",
                quiet=29, since=since),
        ]), "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        sids = [n["sid"] for n in data["nudges"]]
        self.assertEqual(sids, [IDLE], p.stdout)
        self.assertEqual(data["status_skipped"], 1)
        self.assertIn("1 status-skipped", data["summary"])

    def test_a_watching_row_is_unaffected_by_the_status_gate(self):
        """The gate is on the `idle` bucket only: a `watching` row requires a running
        shell (`status: shell`) by construction, and stays a candidate."""
        p = self.run_script(reading([row(sid=OVER, bucket="watching", quiet=31)]))
        self.assertEqual(len(self.nudges(p)), 1, p.stdout)
        self.assertIn("0 status-skipped", p.stdout)


class AnchorTest(_NudgeBase):
    """The park order used to tell a session with no anchored task to run
    `register --task "<your anchored task>"` and `vault-cli task set` on the same
    placeholder. The park binds a wait to an ANCHORED TASK, so for an unanchored
    session it is inapplicable — the same reasoning as the manager-tier skip."""

    def test_an_unanchored_row_is_skipped_while_an_anchored_one_is_nudged(self):
        """With its positive control: same bucket, same status, same duration.
        Deleting the nudge entirely would pass the first half and fail the second."""
        p = self.run_script(reading([
            row(sid=UNANCHORED, bucket="idle", quiet=31),
            row(sid=OVER, bucket="idle", quiet=31),
        ]), "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual([n["sid"] for n in data["nudges"]], [OVER], p.stdout)
        msg = data["nudges"][0]["message"]
        self.assertIn(f"--task 'Task {OVER[:8]}'", msg)
        self.assertIn(f"vault-cli task get 'Task {OVER[:8]}' defer_date", msg)
        self.assertNotIn("<your anchored task>", msg)
        self.assertEqual(data["unanchored"], 1)
        self.assertEqual(data["anchor_unknown"], 0)
        self.assertIn("1 unanchored-skipped", data["summary"])

    def test_a_failed_anchor_search_is_unknown_not_unanchored(self):
        """A search that errored proves nothing about absence. The row is not nudged
        — a nudge would carry no task — and it is counted apart from `unanchored`, so
        an unreadable vault never reads as a fleet of unanchored sessions.

        The failure is injected through PATH, not permissions: `chmod 0` produces a
        read error only for a non-root user, so under uid 0 the same test asserted
        the opposite of what it claims. A shim `rg` that exits 2 is uid-independent
        and is the exact shape `_grep_task` reads as an error."""
        shim = self.tmp / "shim"
        shim.mkdir()
        (shim / "rg").write_text("#!/bin/sh\nexit 2\n")
        (shim / "rg").chmod(0o755)
        p = self.run_script(reading([row(sid=UNANCHORED, bucket="idle", quiet=31)]), "--json",
                            extra_env={"PATH": f"{shim}:{os.environ['PATH']}"})
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data["nudges"], [], p.stdout)
        self.assertEqual(data["anchor_unknown"], 1, p.stdout)
        self.assertEqual(data["unanchored"], 0, p.stdout)

    def test_a_row_with_no_status_key_is_skipped_and_counted(self):
        """A producer that stops emitting `status` must not silently disable the
        nudge for the whole fleet — every `idle` row would read `status-skipped`.
        The count is what makes that visible rather than silent, so a row with the
        key absent is asserted to land in it."""
        bare = row(sid=OVER, bucket="idle", quiet=31)
        bare.pop("status")
        p = self.run_script(reading([bare]), "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data["nudges"], [], p.stdout)
        self.assertEqual(data["status_skipped"], 1, p.stdout)

    def test_an_unanchored_manager_row_is_manager_skipped_not_unanchored(self):
        """The tier gate runs first, so the two skips never double-count one row.
        An unanchored manager must read `manager-skipped`: that is the gate that
        decided, and reporting it as `unanchored` would hide a tier regression."""
        self.arm_manager(f"goal--{MANAGER_BASE}")
        (self.mgr_state / f"goal--{MANAGER_BASE}.session").write_text(f"{MANAGER}\n")
        p = self.run_script(reading([
            row(sid=MANAGER, name=MANAGER_NAME, bucket="idle", quiet=31),
            row(sid=UNANCHORED, bucket="idle", quiet=31),
        ]), "--json")
        data = json.loads(p.stdout)
        self.assertEqual(data["manager"], 1, p.stdout)
        self.assertEqual(data["unanchored"], 1, p.stdout)
        self.assertEqual(data["nudges"], [], p.stdout)

    def test_a_session_handed_off_by_its_task_is_still_anchored(self):
        """`find_task_checked` also answers through `metrics_sessions` when the task's
        `claude_session_id` has moved on. That session's task is real, so it is
        anchored, not skipped."""
        (self.tasks / "Handed Off.md").write_text(
            "---\nclaude_session_id: 11111111-0000-4000-8000-000000000000\n"
            "metrics_sessions:\n    - session_id: " + UNANCHORED + "\n"
            "      started_at: \"2026-11-18T20:00:00+01:00\"\n---\n")
        p = self.run_script(reading([row(sid=UNANCHORED, bucket="idle", quiet=31)]))
        lines = self.nudges(p)
        self.assertEqual(len(lines), 1, p.stdout)
        self.assertIn("--task 'Handed Off'", lines[0])


if __name__ == "__main__":
    unittest.main()