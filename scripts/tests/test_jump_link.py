#!/usr/bin/env python3
"""Tests for scripts/jump-link.py.

The manager commands print this script's output straight into the blocked-by-you
panel, which the operator is meant to act on. So the property worth pinning is not
"it builds a URL" — it is **never a dead link**: every failure path must degrade to
the `/supervisor:jump <N>` command, because a link that looks followable and is not
is worse than the command it replaced. A hand-written URL was the alternative and
it fails exactly this way, silently.

Second property: the token is read at emit time from a 0600 file outside every repo,
never embedded. That is what keeps a committed command file from carrying a secret
that would defeat the CSRF control the token exists for.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
import urllib.parse
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SCRIPTS, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


jl = _load("jump_link", "jump-link.py")

TOKEN = "test-token-not-a-real-secret"


def run_main(argv, token_path=None, extra_env=None):
    """Call main() with the token path pinned; return (rc, stdout)."""
    env = {"JUMP_TOKEN_PATH": token_path if token_path is not None else "/nonexistent/token"}
    env.update(extra_env or {})
    buf = io.StringIO()
    with mock.patch.dict(os.environ, env, clear=False):
        with contextlib.redirect_stdout(buf):
            rc = jl.main(argv)
    return rc, buf.getvalue().strip()


class TestJumpLink(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", delete=False, suffix="-token")
        self.tmp.write(TOKEN + "\n")
        self.tmp.close()
        os.chmod(self.tmp.name, 0o600)

    def tearDown(self):
        os.unlink(self.tmp.name)

    # -- the happy path -------------------------------------------------------

    def test_emits_loopback_link_with_token(self):
        rc, out = run_main(["1346"], token_path=self.tmp.name)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("http://127.0.0.1:1337/jump?pane=1346&t="))
        self.assertTrue(out.endswith(TOKEN))

    def test_output_is_a_single_line(self):
        """The panel renders one row per target; a second line breaks the frame."""
        rc, out = run_main(["1346"], token_path=self.tmp.name)
        self.assertEqual(rc, 0)
        self.assertNotIn("\n", out)

    def test_url_special_token_is_encoded_not_interpolated(self):
        """A token with `&`, `#` or `=` must survive the round trip intact.

        Interpolating it raw lets `&` inject a second query parameter and `#`
        truncate the token at the fragment, so the link fails in a way that
        reads as "the server is down" rather than "the link is malformed".
        Asserting on the RECOVERED token is what makes this test bite: a test
        that only checked the URL contained the token would pass either way.
        """
        nasty = "abc&foo=bar#123"
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write(nasty + "\n")
            path = fh.name
        try:
            rc, out = run_main(["1346"], token_path=path)
            self.assertEqual(rc, 0)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(out).query)
            self.assertEqual(query.get("t"), [nasty])
            self.assertEqual(query.get("pane"), ["1346"])
            self.assertNotIn("foo", query)  # the injected parameter must not exist
            self.assertNotIn("#", out)      # nothing may open a fragment
        finally:
            os.unlink(path)

    def test_malformed_host_and_port_fall_back_to_defaults(self):
        """A bad env value must not produce a link that resolves nowhere."""
        rc, out = run_main(["7"], token_path=self.tmp.name,
                           extra_env={"JUMP_HOST": "http://evil.example.com/x",
                                      "JUMP_PORT": "not-a-port"})
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("http://127.0.0.1:1337/jump?pane=7&t="))

    def test_port_and_host_are_overridable(self):
        rc, out = run_main(["7"], token_path=self.tmp.name,
                           extra_env={"JUMP_PORT": "9999", "JUMP_HOST": "127.0.0.2"})
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("http://127.0.0.2:9999/jump?pane=7&t="))

    # -- the property that matters: never a dead link -------------------------

    def test_missing_token_file_falls_back_to_command(self):
        rc, out = run_main(["1346"], token_path="/nonexistent/token")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "/supervisor:jump 1346")

    def test_empty_token_file_falls_back_to_command(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write("   \n")
            empty = fh.name
        try:
            rc, out = run_main(["1346"], token_path=empty)
            self.assertEqual(rc, 0)
            self.assertEqual(out, "/supervisor:jump 1346")
        finally:
            os.unlink(empty)

    def test_fallback_never_contains_a_url(self):
        """Guards the shape directly: a fallback row must not look clickable."""
        _, out = run_main(["1346"], token_path="/nonexistent/token")
        self.assertNotIn("http", out)

    # -- argument handling ----------------------------------------------------

    def test_non_integer_pane_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            rc = jl.main(["abc"])
        self.assertEqual(rc, 2)

    def test_wrong_arity_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            rc = jl.main([])
        self.assertEqual(rc, 2)

    def test_label_flag_does_not_change_the_pane(self):
        rc, out = run_main(["1346", "--label"], token_path=self.tmp.name)
        self.assertEqual(rc, 0)
        self.assertIn("pane=1346&t=", out)

    def test_clean_title_strips_status_glyph(self):
        self.assertEqual(jl.clean_title("◑ My Session"), "My Session")
        self.assertEqual(jl.clean_title("  "), "(untitled)")
        self.assertEqual(jl.clean_title(None), "(untitled)")


if __name__ == "__main__":
    unittest.main()
