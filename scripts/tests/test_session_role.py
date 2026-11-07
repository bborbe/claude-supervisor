#!/usr/bin/env python3
"""Tests for scripts/session-role.py.

The module answers one question — which `producer_kind` an attention item should
carry — and the tests pin the four decisions that make the answer trustworthy:

  * **the declared value wins.** `cron`, `agent` and `dark-factory` are the
    producer's own statement about itself; only the `session` default is eligible
    for resolution. A resolver that overrode an explicit declaration would
    relabel a cron job as a manager.
  * **`manager` comes from the ledger's explicit `role` field**, and nothing
    else. `role-mode-matrix` Rule 1 says a manager is declared, never derived, so
    an agent row, a 404 and a dead store all answer `session` — never `manager`.
  * **no hardcoded allow-list.** Two session ids with two stubbed roles must
    yield two different answers from the SAME resolver call. A body of
    `return "session"`, or a baked-in set of known manager ids, passes every
    single-id test and fails this one.
  * **the failure direction is "behave as before".** An unreachable ledger, a
    malformed row, or an empty id resolves `session`. A ledger outage must never
    relabel a card as a manager's.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import json
import os
import shutil
import tempfile
import unittest
import urllib.error

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MANAGER_ID = "13d61328-c3cb-45c7-8869-164654093927"
WORKER_ID = "0fc75c81-e117-49ce-82bb-e8fa0dcdf29f"


def load():
    """Load the hyphenated script as a module, the way the producers do."""
    path = os.path.join(SCRIPTS, "session-role.py")
    spec = importlib.util.spec_from_file_location("session_role_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def url_of(request):
    """The URL a stubbed `urlopen` was handed — a Request object or a bare str."""
    return getattr(request, "full_url", request)


class _Response:
    def __init__(self, body):
        # `urlopen` hands back bytes; a str body here would let a decode bug in
        # the module pass the suite while failing against a real store.
        self._body = body.encode("utf-8") if isinstance(body, str) else body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ResolveProducerKindTest(unittest.TestCase):
    def setUp(self):
        self.mod = load()
        self.mod.reset_cache()
        self._original_urlopen = self.mod.urllib.request.urlopen
        self.calls = []
        # The page scan reads the real vault; stub it out so every case below is
        # about the ledger, and the page cases opt in explicitly.
        self._original_scan = self.mod._scan_pages
        self.mod._scan_pages = lambda: set()

    def tearDown(self):
        self.mod.urllib.request.urlopen = self._original_urlopen
        self.mod._scan_pages = self._original_scan
        self.mod.reset_cache()

    def stub(self, role=None, error=None, raw=None):
        """Answer every ledger GET with one row (or raise, or return raw bytes)."""

        def fake_urlopen(request, timeout=None, context=None):
            self.calls.append(url_of(request))
            if error is not None:
                raise error
            if raw is not None:
                return _Response(raw)
            return _Response(json.dumps({"role": role} if role is not None else {}))

        self.mod.urllib.request.urlopen = fake_urlopen

    # --- the declared value wins -------------------------------------------

    def test_explicit_non_session_kind_wins_without_a_ledger_read(self):
        self.stub(role="manager")
        for declared in ("cron", "agent", "dark-factory"):
            self.assertEqual(
                self.mod.resolve_producer_kind(MANAGER_ID, declared), declared,
                f"{declared} must not be overridden by the ledger",
            )
        self.assertEqual(self.calls, [], "an explicit kind must not read the ledger")

    # --- the ledger decides only the `session` default ---------------------

    def test_manager_role_resolves_manager(self):
        self.stub(role="manager")
        self.assertEqual(self.mod.resolve_producer_kind(MANAGER_ID), "manager")

    def test_agent_role_resolves_session(self):
        self.stub(role="agent")
        self.assertEqual(self.mod.resolve_producer_kind(WORKER_ID), "session")

    def test_human_role_resolves_session(self):
        self.stub(role="human")
        self.assertEqual(self.mod.resolve_producer_kind(WORKER_ID), "session")

    def test_empty_role_resolves_session(self):
        self.stub(role="")
        self.assertEqual(self.mod.resolve_producer_kind(WORKER_ID), "session")

    # --- no hardcoded allow-list ------------------------------------------

    def test_two_ids_with_two_roles_yield_two_answers_from_one_resolver(self):
        """The discriminating test: a constant or an allow-list fails it."""
        roles = {MANAGER_ID: "manager", WORKER_ID: "agent"}

        def fake_urlopen(request, timeout=None, context=None):
            url = url_of(request)
            self.calls.append(url)
            session_id = url.rsplit("/", 1)[-1]
            return _Response(json.dumps({"role": roles[session_id]}))

        self.mod.urllib.request.urlopen = fake_urlopen
        self.assertEqual(self.mod.resolve_producer_kind(MANAGER_ID), "manager")
        self.assertEqual(self.mod.resolve_producer_kind(WORKER_ID), "session")
        self.assertEqual(len(self.calls), 2)

    # --- failure direction: behave as before ------------------------------

    def test_absent_ledger_row_resolves_session(self):
        self.stub(error=urllib.error.HTTPError("u", 404, "not found", {}, None))
        self.assertEqual(self.mod.resolve_producer_kind(MANAGER_ID), "session")

    def test_unreachable_ledger_resolves_session(self):
        self.stub(error=urllib.error.URLError("connection refused"))
        self.assertEqual(self.mod.resolve_producer_kind(MANAGER_ID), "session")

    def test_malformed_row_resolves_session(self):
        self.stub(raw=b"not json")
        self.assertEqual(self.mod.resolve_producer_kind(MANAGER_ID), "session")

    def test_non_dict_row_resolves_session(self):
        self.stub(raw=json.dumps(["manager"]).encode())
        self.assertEqual(self.mod.resolve_producer_kind(MANAGER_ID), "session")

    def test_non_string_role_does_not_raise(self):
        """A malformed row must not break a helper documented as never raising."""
        self.stub(raw=json.dumps({"role": 7}).encode())
        self.assertEqual(self.mod.resolve_producer_kind(MANAGER_ID), "session")

    def test_a_failed_read_is_not_cached(self):
        """A transient blip must not pin a manager to `session` for the process."""
        calls = {"n": 0}

        def fake_urlopen(request, timeout=None, context=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.URLError("blip")
            return _Response(json.dumps({"role": "manager"}))

        self.mod.urllib.request.urlopen = fake_urlopen
        self.assertEqual(self.mod.resolve_producer_kind(MANAGER_ID), "session")
        self.assertEqual(self.mod.resolve_producer_kind(MANAGER_ID), "manager")
        self.assertEqual(calls["n"], 2, "the failed read must be retried, not memoized")

    def test_session_id_is_quoted_into_the_path(self):
        """A caller-supplied id must address one row, not rewrite the request."""
        self.stub(role="manager")
        self.mod.resolve_producer_kind("a/../b?x=1#f")
        self.assertTrue(
            self.calls[0].endswith("/session-ledger/a%2F..%2Fb%3Fx%3D1%23f"),
            self.calls[0],
        )

    # --- the goal-page declaration ----------------------------------------
    # Measured 2026-11-07: only 6 of 11 live manager sessions had a ledger row.
    # The hand-started and `/supervisor:open` wezterm-branch managers write none,
    # and their goal page is where they declare themselves.

    def test_a_goal_page_declaration_resolves_manager_without_a_ledger_row(self):
        self.stub(error=urllib.error.HTTPError("u", 404, "not found", {}, None))
        self.mod._scan_pages = lambda: {MANAGER_ID.lower()}
        self.assertEqual(self.mod.resolve_producer_kind(MANAGER_ID), "manager")

    def test_a_goal_page_declaration_does_not_override_an_explicit_kind(self):
        self.stub(role="manager")
        self.mod._scan_pages = lambda: {MANAGER_ID.lower()}
        self.assertEqual(self.mod.resolve_producer_kind(MANAGER_ID, "cron"), "cron")

    def test_one_page_walk_serves_every_session(self):
        """A per-session walk measured 257 ms; the index must be shared."""
        self.stub(error=urllib.error.URLError("down"))
        walks = {"n": 0}

        def scan():
            walks["n"] += 1
            return {MANAGER_ID.lower()}

        self.mod._scan_pages = scan
        self.assertEqual(self.mod.resolve_producer_kind(MANAGER_ID), "manager")
        self.assertEqual(self.mod.resolve_producer_kind(WORKER_ID), "session")
        self.assertEqual(walks["n"], 1, "the second session must reuse the index")

    def test_empty_session_id_resolves_session_without_a_read(self):
        self.stub(role="manager")
        self.assertEqual(self.mod.resolve_producer_kind(""), "session")
        self.assertEqual(self.calls, [])

    def test_whitespace_session_id_resolves_session_without_a_read(self):
        self.stub(role="manager")
        self.assertEqual(self.mod.resolve_producer_kind("   "), "session")
        self.assertEqual(self.calls, [])

    # --- the memo ---------------------------------------------------------

    def test_role_is_memoized_per_session(self):
        self.stub(role="manager")
        self.mod.resolve_producer_kind(MANAGER_ID)
        self.mod.resolve_producer_kind(MANAGER_ID)
        self.assertEqual(len(self.calls), 1, "a second read of the same session is a cache hit")

    def test_reset_cache_forces_a_fresh_read(self):
        self.stub(role="manager")
        self.mod.resolve_producer_kind(MANAGER_ID)
        self.mod.reset_cache()
        self.mod.resolve_producer_kind(MANAGER_ID)
        self.assertEqual(len(self.calls), 2)


class ScanPagesTest(unittest.TestCase):
    """The REAL filesystem walk — every other case stubs it out.

    ⚠️ The distinction under test is the one the review caught: the page must
    declare the session through the `claude_session_id:` FRONTMATTER FIELD, not
    by merely mentioning the id. A goal page that lists a worker's id in
    `metrics_sessions` or a Progress line would otherwise label that worker's
    card `manager` — the inverse of the property this module protects.
    """

    def setUp(self):
        self.mod = load()
        self.mod.reset_cache()
        self._original_dir = self.mod.OBSIDIAN_DIR
        self.tmp = tempfile.mkdtemp()
        self.mod.OBSIDIAN_DIR = self.tmp

    def tearDown(self):
        self.mod.OBSIDIAN_DIR = self._original_dir
        self.mod.reset_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def page(self, vault, sub, name, body):
        directory = os.path.join(self.tmp, vault, sub)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, name), "w", encoding="utf-8") as fh:
            fh.write(body)

    def test_frontmatter_stamp_declares_the_manager(self):
        self.page("v", "24 Goals", "G.md", f"---\nclaude_session_id: {MANAGER_ID}\n---\n")
        self.assertIn(MANAGER_ID.lower(), self.mod._scan_pages())

    def test_a_bare_mention_does_not_declare(self):
        """The review's defect: an id listed anywhere is not a declaration."""
        self.page(
            "v", "24 Goals", "G.md",
            f"---\nclaude_session_id: {MANAGER_ID}\n---\n\n"
            f"metrics_sessions:\n  - session_id: {WORKER_ID}\n",
        )
        stamps = self.mod._scan_pages()
        self.assertNotIn(WORKER_ID.lower(), stamps, "a listed worker must not read manager")
        self.assertIn(MANAGER_ID.lower(), stamps)

    def test_an_unstamped_page_declares_nothing(self):
        self.page("v", "24 Goals", "G.md", "---\ntitle: x\n---\n")
        self.assertNotIn(MANAGER_ID.lower(), self.mod._scan_pages())

    def test_a_topic_page_stamp_also_declares(self):
        self.page("v", "23 Topics", "T.md", f"---\nclaude_session_id: {MANAGER_ID}\n---\n")
        self.assertIn(MANAGER_ID.lower(), self.mod._scan_pages())

    def test_an_unreadable_root_declares_nothing(self):
        self.mod.OBSIDIAN_DIR = os.path.join(self.tmp, "does-not-exist")
        self.assertEqual(self.mod._scan_pages(), set())


if __name__ == "__main__":
    unittest.main()
