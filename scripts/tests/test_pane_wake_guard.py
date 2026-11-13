"""pane-wake-guard.py — the pane a fleet wake may type into, and every one it must not.

The guard is the only thing between `/supervisor:fleet-triage` and another session's
keyboard. `send_agent_message` already refuses a non-empty composer (which covers a
`❯ 1.` option row), but a mid-turn pane shows an empty composer too, so the spinner case
is this guard's alone. Each skip marker gets a fixture, and the anchoring rule gets the
counter-case: a modal marker QUOTED IN PROSE is not a modal.
"""
import importlib.util
import pathlib
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "pane-wake-guard.py"
spec = importlib.util.spec_from_file_location("pane_wake_guard", SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

NBSP = " "
STATUS = "  ⏵⏵ auto mode on (shift+tab to cycle)"

IDLE = "\n".join([
    "⏺ Done. Subtask 2 is next.",
    "",
    "╭" + "─" * 20,
    "❯" + NBSP,
    "╰" + "─" * 20,
    STATUS,
])

SPINNER = "\n".join([
    "⏺ Reading the file.",
    "✶ Determining… (12s · ↓ 1.2k tokens · esc to interrupt)",
    "❯" + NBSP,
    STATUS,
])

SPINNER_NO_HINT = "\n".join([
    "✻ Newspapering… (3m 4s)",
    "❯" + NBSP,
    STATUS,
])

MODAL_SELECT = "\n".join([
    "Which tier should run fleet-triage?",
    "❯ 1. New dedicated tier",
    "  2. Split it out",
    "",
    "Enter to select · ↑/↓ to navigate · Esc to cancel",
    STATUS,
])

MODAL_PERMISSION = "\n".join([
    "Bash command",
    "  git push origin fix/x",
    "Do you want to proceed?",
    "  1. Yes",
    "  2. No",
    "",
    "   Esc to cancel · Tab to amend",
])

OPTION_LIST_ONLY = "\n".join([
    "Pick one:",
    "❯ 1. Continue",
    "  2. Stop",
    STATUS,
])

QUOTED_MARKERS = "\n".join([
    "⏺ The guard skips a pane whose line begins with `Enter to select` or `Esc to cancel`,",
    "  and the drill quoted “❯ 1. Yes” from the fixture.",
    "❯" + NBSP,
    STATUS,
])

DIRTY_COMPOSER = "\n".join([
    "⏺ Waiting for your answer.",
    "❯ draft ok",
    STATUS,
])

NO_COMPOSER = "\n".join([
    "$ ls",
    "README.md",
])

MACHINE_WAIT = "\n".join([
    "\U0001f7e1 WAITING",
    "   What:     CI run",
    "⏰ Ends:   checks COMPLETED (Monitor, b1)",
    "❯" + NBSP,
    STATUS,
])

BOXED_MODAL = "\n".join([
    "│ Do you want to make this edit?",
    "│ ❯ 1. Yes",
    "│   2. No",
    "│ Esc to cancel",
])


# Captured 2026-11-08 from a live idle pane with `get-text --escapes`: the composer holds
# Claude Code's next-prompt suggestion, drawn dim (SGR 2). Plain `get-text` renders it
# exactly like typed text.
SUGGESTION_ESCAPED = "\n".join([
    "\x1b[39m─" * 3,
    "\x1b[39m❯ \x1b(B\x1b[0;2mcheck the review again",
    "\x1b[0m  ⏵⏵ auto mode on (shift+tab to cycle)",
])

TYPED_ESCAPED = "\n".join([
    "\x1b[39m❯ \x1b(B\x1b[0mdraft ok",
    STATUS,
])

DIM_THEN_TYPED = "\n".join([
    "\x1b[39m❯ \x1b[2mghost\x1b[22m typed",
    STATUS,
])


class VerdictTest(unittest.TestCase):
    def test_dim_suggestion_is_not_typed_text(self):
        self.assertEqual(guard.verdict(SUGGESTION_ESCAPED), "wake:suggestion")

    def test_operator_ask_closer_skips(self):
        text = "\n".join([
            "\U0001f535 READY · nothing running · you decide",
            "\U0001f464 You: pick — 1. build it (recommended) · 2. hold",
            "❯" + NBSP,
            STATUS,
        ])
        self.assertEqual(guard.verdict(text), "skip:operator-ask")

    def test_nothing_closer_does_not_skip(self):
        text = "\n".join([
            "\U0001f464 You:    nothing",
            "❯" + NBSP,
            STATUS,
        ])
        self.assertEqual(guard.verdict(text), "wake")

    def test_empty_read_is_unclassified(self):
        self.assertEqual(guard.verdict("  \n\n"), "skip:unclassified")

    def test_typed_text_with_escapes_is_not_empty(self):
        self.assertEqual(guard.verdict(TYPED_ESCAPED), "skip:composer-not-empty")

    def test_any_undimmed_char_counts_as_typed(self):
        self.assertEqual(guard.verdict(DIM_THEN_TYPED), "skip:composer-not-empty")

    def test_truecolor_typed_text_is_not_a_suggestion(self):
        # The `2` in `38;2;r;g;b` is a colour sub-parameter, not faint. Reading it as faint
        # would call coloured typed text a suggestion — the dangerous direction.
        typed_truecolor = "\x1b[39m❯ \x1b[38;2;200;100;50mdraft ok\x1b[0m\n" + STATUS
        self.assertEqual(guard.verdict(typed_truecolor), "skip:composer-not-empty")

    def test_dim_then_reset_then_typed_is_typed(self):
        self.assertFalse(guard.is_dim_only("\x1b[0;2mghost\x1b[0m real"))

    def test_idle_empty_composer_wakes(self):
        self.assertEqual(guard.verdict(IDLE), "wake")

    def test_spinner_with_interrupt_hint_skips(self):
        self.assertEqual(guard.verdict(SPINNER), "skip:spinner")

    def test_spinner_line_without_hint_skips(self):
        self.assertEqual(guard.verdict(SPINNER_NO_HINT), "skip:spinner")

    def test_selection_modal_skips(self):
        # The option row precedes the footer, and either alone must refuse; the footer
        # is the marker the gate-relay rules name, so it is asserted by its own fixture
        # below with the option row removed.
        self.assertIn(guard.verdict(MODAL_SELECT), ("skip:modal-select", "skip:option-list"))

    def test_selection_footer_alone_skips(self):
        footer_only = MODAL_SELECT.replace("❯ 1. New dedicated tier", "  1. New dedicated tier")
        self.assertEqual(guard.verdict(footer_only), "skip:modal-select")

    def test_permission_modal_skips(self):
        self.assertEqual(guard.verdict(MODAL_PERMISSION), "skip:modal-cancel")

    def test_option_list_skips(self):
        self.assertEqual(guard.verdict(OPTION_LIST_ONLY), "skip:option-list")

    def test_boxed_modal_skips(self):
        self.assertIn(guard.verdict(BOXED_MODAL), ("skip:option-list", "skip:modal-cancel"))

    def test_markers_quoted_in_prose_do_not_skip(self):
        # The anchoring rule's counter-case: a marker mid-line is content, not a render.
        self.assertEqual(guard.verdict(QUOTED_MARKERS), "wake")

    def test_dirty_composer_skips(self):
        self.assertEqual(guard.verdict(DIRTY_COMPOSER), "skip:composer-not-empty")

    def test_no_composer_skips(self):
        self.assertEqual(guard.verdict(NO_COMPOSER), "skip:no-composer")

    def test_machine_wait_skips(self):
        self.assertEqual(guard.verdict(MACHINE_WAIT), "skip:machine-wait")

    def test_machine_wait_passes_with_the_sweep_gate_carve_out(self):
        # A sweep-gate wake is precisely what a manager's declared machine wait is
        # waiting for; without this the gate could never wake a resting goal manager.
        self.assertEqual(guard.verdict(MACHINE_WAIT, allow_machine_wait=True), "wake")

    def test_operator_ask_still_blocks_with_the_carve_out(self):
        # The operator-ask check runs AFTER the machine-wait branch, so the carve-out
        # must not swallow it.
        text = MACHINE_WAIT + "\n👤 You: approve: rm -rf /"
        self.assertEqual(guard.verdict(text, allow_machine_wait=True), "skip:operator-ask")

    def test_unreadable_is_an_error(self):
        self.assertEqual(guard.verdict(None), "error:unreadable")

    def test_sleeping_subject_skips_an_otherwise_wakeable_pane(self):
        # A date-gated sleeper is idle BY DEFINITION — empty composer, no `👤 You:` ask,
        # nothing on screen that tells it from a lapse. Only the state does.
        self.assertEqual(guard.verdict(IDLE, subject_state="SLEEPING"), "skip:sleeping")

    def test_sleeping_subject_skips_a_dirty_composer_too(self):
        self.assertEqual(guard.verdict(DIRTY_COMPOSER, subject_state="SLEEPING"), "skip:sleeping")

    def test_sleeping_subject_skips_even_an_unreadable_pane(self):
        # The state is authoritative and independent of the screen: a failed pane read
        # must not turn a correct sleep into a wakeable target.
        self.assertEqual(guard.verdict(None, subject_state="SLEEPING"), "skip:sleeping")

    def test_unreadable_state_fails_closed(self):
        # A subject RESOLVED but its state could not be read. 2026-11-13: three wakes
        # landed inside a SLEEPING window because this case fell through to `wake`.
        self.assertEqual(guard.verdict(IDLE, subject_state=guard.STATE_UNREADABLE),
                         "skip:state-unreadable")

    def test_unreadable_state_is_distinct_from_no_subject(self):
        # No subject resolved at all is NOT the same claim, and must still wake — a
        # hand-opened tab has no state to read and is a legitimate target.
        self.assertEqual(guard.verdict(IDLE, subject_state=None), "wake")

    def test_awake_subject_still_wakes_the_same_pane(self):
        # The negative direction, named for itself: the SAME screen with any non-sleeping
        # state must still wake, or the new arm would have swallowed every wake while
        # reading as success.
        for state in ("OK", "STALE", "STOPPED", "UNARMED", "INVALID", None):
            with self.subTest(state=state):
                self.assertEqual(guard.verdict(IDLE, subject_state=state), "wake")


class ExitStatusTest(unittest.TestCase):
    def _run(self, text, tmp):
        p = pathlib.Path(tmp) / "pane.txt"
        p.write_text(text, encoding="utf-8")
        return guard.main(["--text-file", str(p)])

    def test_exit_codes(self):
        import tempfile
        from contextlib import redirect_stdout
        import io
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(IDLE, tmp), 0)
            self.assertEqual(self._run(SPINNER, tmp), 1)
            self.assertEqual(guard.main(["--text-file", str(pathlib.Path(tmp) / "missing.txt")]), 2)

    def test_subject_state_flag_skips_an_idle_pane(self):
        import tempfile
        from contextlib import redirect_stdout
        import io
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            p = pathlib.Path(tmp) / "pane.txt"
            p.write_text(IDLE, encoding="utf-8")
            self.assertEqual(guard.main(["--text-file", str(p), "--subject-state", "SLEEPING"]), 1)
            self.assertEqual(guard.main(["--text-file", str(p), "--subject-state", "OK"]), 0)


if __name__ == "__main__":
    unittest.main()
