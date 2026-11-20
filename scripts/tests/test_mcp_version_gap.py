"""mcp-version-gap.py — the two branches, and the fold that would make either one a lie.

The load-bearing rule is that **the probe must be able to say `agreement`**. A probe that
always reports divergence is the same defect as one that always reports agreement: it carries
no information, and it would pass an e2e that only ever pastes the divergent reading. So
`AgreementBranchTest` feeds the same version to both sides and requires `agreement` with gap
`0` and the tag named, while `DivergenceBranchTest` feeds the live shape and requires both
versions quoted — the pinned-old case is what stops a constant from passing.

The second rule is that **`unread` is never folded into either verdict**. A failed `lsof`, a
truncated `comm`, a missing `go`, an unreachable `gh` — each must read `unread` with its own
error, because `agreement` from a probe that could not read its reference and `divergent` from
a read that never happened are the two silent directions this instrument exists to close.

The third is that **the binary comes from the listening port**. The config names a URL, so a
probe reading a config path, or `git describe` in a checkout, answers a different question
about a different artifact. `ResolutionTest` pins the port → pid → path chain and refuses a
truncated `comm`.
"""
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest
from contextlib import redirect_stdout

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "mcp-version-gap.py"
spec = importlib.util.spec_from_file_location("mcp_version_gap", SCRIPT)
gap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gap)

# The live reading, 2026-11-20 — the shape this probe was filed for.
LIVE_BUILD_INFO = """\
/Users/bborbe/.local/bin/claude-wrapper: go1.25.3
\tpath\tgithub.com/bborbe/claude-wrapper
\tmod\tgithub.com/bborbe/claude-wrapper\tv0.45.2-0.20261120032107-c84c80e5ee16\th1:abc=
\tbuild\t-buildmode=exe
\tbuild\tvcs.revision=c84c80e5ee169ac148856e6020e7e43a3873364b
\tbuild\tvcs.time=2026-11-20T03:21:07Z
\tbuild\tvcs.modified=false
"""

RELEASES_JSON = json.dumps([
    {"tagName": "v0.48.0"},
    {"tagName": "v0.47.0"},
    {"tagName": "v0.46.0"},
    {"tagName": "v0.45.2"},
])

RELEASES_PLAIN = """\
v0.48.0\tLatest\tv0.48.0\t2026-11-20T09:05:57Z
v0.47.0\t\tv0.47.0\t2026-11-20T08:23:31Z
v0.46.0\t\tv0.46.0\t2026-11-20T03:22:08Z
"""

LSOF_TWO_ROWS_ONE_PID = """\
COMMAND     PID   USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME
claude-wr 13094 bborbe  145u  IPv4 0xd903740becef3c6b      0t0  TCP 127.0.0.1:18090 (LISTEN)
claude-wr 13094 bborbe  146u  IPv6 0xd903740becef3c6c      0t0  TCP [::1]:18090 (LISTEN)
"""


class ParseTest(unittest.TestCase):
    def test_build_info_carries_the_module_version_and_revision(self):
        info = gap.parse_build_info(LIVE_BUILD_INFO)
        self.assertEqual(info["module"], "github.com/bborbe/claude-wrapper")
        self.assertEqual(info["version"], "v0.45.2-0.20261120032107-c84c80e5ee16")
        self.assertEqual(info["revision"], "c84c80e5ee169ac148856e6020e7e43a3873364b")
        self.assertEqual(info["vcs_time"], "2026-11-20T03:21:07Z")

    def test_a_devel_build_reads_as_devel_not_as_a_version(self):
        info = gap.parse_build_info(
            "/tmp/x: go1.25.3\n\tpath\tgithub.com/bborbe/claude-wrapper\n"
            "\tmod\tgithub.com/bborbe/claude-wrapper\t(devel)\t\n"
            "\tbuild\tvcs.revision=abc123\n")
        self.assertEqual(info["version"], "(devel)")

    def test_releases_parse_from_json_and_from_plain_output(self):
        self.assertEqual(gap.parse_releases(RELEASES_JSON),
                         ["v0.48.0", "v0.47.0", "v0.46.0", "v0.45.2"])
        self.assertEqual(gap.parse_releases(RELEASES_PLAIN),
                         ["v0.48.0", "v0.47.0", "v0.46.0"])

    def test_lsof_skips_the_header_and_dedupes_a_dual_stack_listener(self):
        self.assertEqual(gap.parse_lsof_listeners(LSOF_TWO_ROWS_ONE_PID), [13094])

    def test_lsof_keeps_two_distinct_pids(self):
        text = ("COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
                "a 111 u 1u IPv4 0x1 0t0 TCP 127.0.0.1:18090 (LISTEN)\n"
                "b 222 u 1u IPv4 0x2 0t0 TCP 127.0.0.1:18090 (LISTEN)\n")
        self.assertEqual(gap.parse_lsof_listeners(text), [111, 222])


class ResolutionTest(unittest.TestCase):
    def test_the_pid_comes_from_the_listening_port(self):
        pids, error = gap.resolve_pid(18090, lsof=LSOF_TWO_ROWS_ONE_PID)
        self.assertIsNone(error)
        self.assertEqual(pids, [13094])

    def test_nothing_listening_is_an_error_not_an_empty_answer(self):
        pids, error = gap.resolve_pid(18090, lsof="COMMAND PID USER\n")
        self.assertEqual(pids, [])
        self.assertIn("nothing listens", error)

    def test_a_truncated_comm_is_refused_rather_than_read_as_a_path(self):
        path, error = gap.resolve_binary(13094, ps="claude-wr\n")
        self.assertIsNone(path)
        self.assertIn("truncated command name", error)

    def test_a_full_path_is_accepted(self):
        path, error = gap.resolve_binary(13094, ps="/Users/bborbe/.local/bin/claude-wrapper\n")
        self.assertEqual(path, "/Users/bborbe/.local/bin/claude-wrapper")
        self.assertIsNone(error)


class DivergenceBranchTest(unittest.TestCase):
    """The live shape: a pseudo-version against a released tag."""

    def test_a_pseudo_version_diverges_and_quotes_both_sides(self):
        reading = gap.compare("v0.45.2-0.20261120032107-c84c80e5ee16",
                              ["v0.48.0", "v0.47.0", "v0.46.0", "v0.45.2"])
        self.assertEqual(reading["state"], gap.DIVERGENT)
        self.assertEqual(reading["running_kind"], "pseudo")
        self.assertEqual(reading["running_base"], "v0.45.2")
        self.assertEqual(reading["running"], "v0.45.2-0.20261120032107-c84c80e5ee16")
        self.assertEqual(reading["released"], "v0.48.0")
        self.assertEqual(reading["gap"], 3)

    def test_an_older_release_diverges_with_a_counted_gap(self):
        reading = gap.compare("v0.46.0", ["v0.48.0", "v0.47.0", "v0.46.0"])
        self.assertEqual(reading["state"], gap.DIVERGENT)
        self.assertEqual(reading["gap"], 2)

    def test_a_devel_build_never_agrees(self):
        reading = gap.compare("(devel)", ["v0.48.0"])
        self.assertEqual(reading["state"], gap.DIVERGENT)
        self.assertEqual(reading["running_kind"], "devel")
        self.assertIsNone(reading["gap"])

    def test_a_pseudo_version_on_the_latest_base_still_diverges(self):
        # Built from a commit after the tag: no tag points at it, so it is not the release.
        reading = gap.compare("v0.48.0-0.20261120120000-deadbeef1234", ["v0.48.0"])
        self.assertEqual(reading["state"], gap.DIVERGENT)
        self.assertEqual(reading["gap"], 0)
        self.assertEqual(reading["released"], "v0.48.0")


class AgreementBranchTest(unittest.TestCase):
    """The false-negative guard: the same version on both sides must read `agreement`."""

    def test_the_same_release_tag_on_both_sides_agrees_with_gap_zero(self):
        reading = gap.compare("v0.48.0", ["v0.48.0", "v0.47.0"])
        self.assertEqual(reading["state"], gap.AGREEMENT)
        self.assertEqual(reading["gap"], 0)
        self.assertEqual(reading["released"], "v0.48.0")

    def test_probe_agrees_when_both_sides_are_fed_the_same_version(self):
        # The agreement proof at unit level: no live tree is pinned, both sides carry the
        # same version, and the reading must be agreement rather than a constant divergence.
        result = gap.probe(running_override="v0.48.0", released_override="v0.48.0")
        self.assertEqual(result["state"], gap.AGREEMENT)
        self.assertEqual(result["gap"], 0)
        self.assertEqual(result["released"], "v0.48.0")
        self.assertTrue(result["overrides"]["running"])
        self.assertTrue(result["overrides"]["released"])

    def test_the_agreement_reading_names_the_tag(self):
        result = gap.probe(running_override="v0.48.0", released_override="v0.48.0")
        line = gap.render_line(result)
        self.assertIn("agreement, gap 0", line)
        self.assertIn("v0.48.0", line)


class UnreadTest(unittest.TestCase):
    def test_no_released_tag_is_unread_not_agreement(self):
        reading = gap.compare("v0.48.0", [])
        self.assertEqual(reading["state"], gap.UNREAD)
        self.assertIn("no released tag", reading["error"])

    def test_no_running_version_is_unread_not_divergent(self):
        reading = gap.compare(None, ["v0.48.0"])
        self.assertEqual(reading["state"], gap.UNREAD)
        self.assertIn("no running version", reading["error"])

    def test_probe_without_a_listener_reports_unread_with_the_error(self):
        result = gap.probe(port=18090, lsof="COMMAND PID USER\n")
        self.assertEqual(result["state"], gap.UNREAD)
        self.assertIn("nothing listens", result["error"])

    def test_a_failed_release_read_is_unread_not_divergent(self):
        result = gap.probe(binary="/Users/bborbe/.local/bin/claude-wrapper",
                           build_info_dump=LIVE_BUILD_INFO, releases_dump="[]")
        self.assertEqual(result["state"], gap.UNREAD)
        self.assertIn("no tags", result["error"])


class RenderTest(unittest.TestCase):
    def test_the_report_quotes_both_versions_and_names_the_state(self):
        result = gap.probe(binary="/Users/bborbe/.local/bin/claude-wrapper", pid=13094,
                           build_info_dump=LIVE_BUILD_INFO, releases_dump=RELEASES_JSON)
        report = gap.render(result)
        self.assertIn("MCP SERVER VERSION: divergent", report)
        self.assertIn("v0.45.2-0.20261120032107-c84c80e5ee16", report)
        self.assertIn("v0.48.0", report)
        self.assertIn("3 release(s) behind", report)

    def test_the_line_form_names_the_state_and_the_gap(self):
        result = gap.probe(binary="/Users/bborbe/.local/bin/claude-wrapper", pid=13094,
                           build_info_dump=LIVE_BUILD_INFO, releases_dump=RELEASES_JSON)
        line = gap.render_line(result)
        self.assertIn("DIVERGENT, gap 3", line)
        self.assertIn("commit c84c80e5ee16", line)


class MainTest(unittest.TestCase):
    def _write(self, text, suffix):
        handle = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False)
        handle.write(text)
        handle.close()
        self.addCleanup(pathlib.Path(handle.name).unlink)
        return handle.name

    def _argv(self, build, releases, *extra):
        return ["--binary", "/Users/bborbe/.local/bin/claude-wrapper", "--pid", "13094",
                "--build-info", build, "--releases-json", releases, *extra]

    def test_divergence_exits_one(self):
        build = self._write(LIVE_BUILD_INFO, ".txt")
        releases = self._write(RELEASES_JSON, ".json")
        out = io.StringIO()
        with redirect_stdout(out):
            code = gap.main(self._argv(build, releases, "--line"))
        self.assertEqual(code, 1)
        self.assertIn("DIVERGENT", out.getvalue())

    def test_agreement_exits_zero(self):
        build = self._write(LIVE_BUILD_INFO, ".txt")
        releases = self._write(RELEASES_JSON, ".json")
        out = io.StringIO()
        with redirect_stdout(out):
            code = gap.main(self._argv(build, releases, "--running-version", "v0.48.0", "--line"))
        self.assertEqual(code, 0)
        self.assertIn("agreement, gap 0", out.getvalue())

    def test_unread_exits_two(self):
        build = self._write(LIVE_BUILD_INFO, ".txt")
        releases = self._write("[]", ".json")
        out = io.StringIO()
        with redirect_stdout(out):
            code = gap.main(self._argv(build, releases, "--line"))
        self.assertEqual(code, 2)
        self.assertIn("UNREAD", out.getvalue())

    def test_json_carries_the_reading_fields(self):
        build = self._write(LIVE_BUILD_INFO, ".txt")
        releases = self._write(RELEASES_JSON, ".json")
        out = io.StringIO()
        with redirect_stdout(out):
            gap.main(self._argv(build, releases, "--json"))
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["state"], "divergent")
        self.assertEqual(payload["running"], "v0.45.2-0.20261120032107-c84c80e5ee16")
        self.assertEqual(payload["released"], "v0.48.0")
        self.assertEqual(payload["gap"], 3)


class PostGuardTest(unittest.TestCase):
    """Only a MEASURED divergence reaches the board — never a fed one, never an agreement."""

    def test_an_agreement_posts_nothing(self):
        result = gap.probe(running_override="v0.48.0", released_override="v0.48.0")
        item_id, error = gap.post(result)
        self.assertIsNone(item_id)
        self.assertIn("only a divergence", error)

    def test_a_fed_divergence_posts_nothing(self):
        result = gap.probe(running_override="v0.1.0", released_override="v0.48.0")
        self.assertEqual(result["state"], gap.DIVERGENT)
        item_id, error = gap.post(result)
        self.assertIsNone(item_id)
        self.assertIn("FED reading", error)

    def test_an_unread_reading_posts_nothing(self):
        result = gap.probe(port=18090, lsof="COMMAND PID USER\n")
        item_id, error = gap.post(result)
        self.assertIsNone(item_id)
        self.assertIn("only a divergence", error)


if __name__ == "__main__":
    unittest.main()
