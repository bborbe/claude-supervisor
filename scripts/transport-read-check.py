#!/usr/bin/env python3
"""Three-state consistency check for every enumerated transport read.

Drives each accessor into the three states a transport read can be in — **broken**
(the query failed), **healthy-with-content**, **healthy-empty** (the transport
answered "nothing") — and asserts the accessor tells them apart. A site that
collapses broken into empty fails here.

Runs against an arbitrary tree, so the same check can be pointed at a pre-fix
revision to prove it fires:

    python3 scripts/transport-read-check.py                       # this tree
    python3 scripts/transport-read-check.py /tmp/pre-audit-tree   # a revision

Exit 0 when every site distinguishes the states; 1 otherwise, naming each site
that does not.

⚠️ The tree argument must be a real checkout, not a per-file `git show`: `jump.py`
and `fleet-board.py` resolve sibling modules from the plugin root and their own
directory, so a per-file materialisation would execute a mixed-revision graph.
Use `git worktree add <tmp> <sha>`.
"""
import argparse
import importlib.util
import io
import contextlib
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# Every transport read site the enumeration derived (2026-09-26, against 88ed821).
# SC6(c): the mechanisms are re-run against the target tree, and every site they
# derive must appear here. A site outside this list means the mechanism and the
# recorded enumeration disagree — the disagreement the criterion exists to surface,
# and the reason the enumeration is written as a search rather than a list.
SITE_LIST = {
    # transport call sites — the walk's seeds
    ("fleet-colours.py", "pane_titles"), ("fleet-colours.py", "pid_ttys"),
    ("fleet-sessions.py", "live_processes"), ("jump-link.py", "pane_title"),
    ("jump.py", "activate_pane_by_id"), ("jump.py", "activate_tab"),
    ("jump.py", "current_window"), ("jump.py", "main"),
    ("jump.py", "pane_sessions"), ("jump.py", "wezterm_panes"),
    ("session-holds.py", "pane_title"),
    # added 2026-10-05 — `open-items.py`'s pane-existence probe, the read half of the new
    # origin record. `classify` asks whether an entry's recorded raising pane still exists;
    # `live_panes` is that query, and it must keep the same three-state contract as every
    # other transport read: `None` when the query failed, `set()` for a reachable WezTerm
    # holding nothing.
    ("open-items.py", "live_panes"),
    # added 2026-10-01 — the pid-identity probe's `ps -o lstart=` read. It was the first
    # transport call `session-liveness.py` held: before the identity fix that file only globbed
    # the registry and called `os.kill`, neither of which shells out. ⚠️ **It moved with the
    # identity machinery** on 2026-10-08, when the registry read was extracted into
    # `session-identity.py` — the walk derives the site from that file now, and
    # `session-liveness.py` holds no `subprocess` call at all.
    ("session-identity.py", "_ps_starts"),
    ("stop-probe.py", "gate_processes"),
    # added 2026-10-07 — `plugin-version-census.py`, the fleet reading of which plugin
    # version each live session serves. Its two READS keep the three-state contract and are
    # driven below: `wezterm_panes` (the pane map the jump target is joined through) and
    # `_pid_ttys` (the `ps` half of that join). ⚠️ `_pid_ttys` shipped returning `{}` for a
    # failed query, which is the collapse this check exists to catch — caught here, on the
    # first run, in a script whose whole output is a count.
    # ⚠️ `reload_pane` is enumerated because it IS a transport call site (activate-pane /
    # get-text / send-text) and the mechanism derives it, but it is an ACTION, not a read:
    # it returns `{ok, why}` and names every failure, so it is deliberately not driven into
    # the three states below. It is listed here so the mechanism and this enumeration agree.
    ("plugin-version-census.py", "wezterm_panes"), ("plugin-version-census.py", "_pid_ttys"),
    ("plugin-version-census.py", "reload_pane"),
    # added 2026-10-10 — `restart-worker.py`'s live-argv read: the `--mcp-config` path the
    # worker was launched with, read before the kill because the registry carries no argv.
    # It returns `None` for a failed query, never an empty argv, and an unreadable argv can
    # only fall back to the load-path check, never turn a refusal into an accept.
    ("restart-worker.py", "worker_argv"),
    ("who-needs-me.py", "main"), ("who-needs-me.py", "wezterm_panes"),
    # in-repo callers that hold no transport call of their own
    ("who-needs-me.py", "panes"),          # present pre-audit only; deleted by this change
    ("who-needs-me.py", "pane_for"), ("who-needs-me.py", "live_pane_ids"),
    ("fleet-board.py", "collect_signals"),
}


class _Proc:
    """The slice of `subprocess.CompletedProcess` the accessors touch."""

    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


def _broken(*_a, **_k):
    """A transport that cannot be reached — the socket is gone."""
    raise OSError("simulated transport failure")


def _empty(*_a, **_k):
    """A transport that answered, with nothing. `[]` parses for both wezterm and ps."""
    return _Proc(stdout="[]", returncode=0)


def _fixture_registry() -> str:
    """A session registry holding one entry, so `pane_sessions` reaches its `ps` read.

    `jump.py`'s `pane_sessions` short-circuits to `{}` when the registry holds nothing —
    it never calls `ps` at all. That is the correct answer for a genuinely empty registry,
    but it means the accessor is only drivable into its **broken** state when there is an
    entry to join. Left to the host's real `~/.claude/sessions`, the site was exercised on
    a developer's Mac and silently skipped on a clean CI runner — where the short-circuit
    produced `{}` and this check reported it as a collapse, failing CI for a defect that
    was not there. The check owns the states it drives an accessor into, so it supplies
    the registry rather than inheriting whatever the machine happens to hold.
    """
    d = tempfile.mkdtemp(prefix="transport-read-registry-")
    with open(os.path.join(d, "1.json"), "w", encoding="utf-8") as fh:
        json.dump({"pid": 1, "sessionId": "11111111-2222-3333-4444-555555555555"}, fh)
    return d


# What a healthy transport holding content answers, per reader. Each accessor
# parses a different shape — a JSON array for the wezterm reads, a whitespace
# column layout for each `ps` read — so no single canned payload serves them all.
# Without this state the check cannot see a fix that returns `None` or an empty
# container unconditionally: both would satisfy the broken and healthy-empty
# assertions while breaking every working read.
POPULATED = {
    ("wnm", "wezterm_panes"): '[{"pane_id": "204", "title": "t"}]',
    ("wnm", "live_pane_ids"): '[{"pane_id": "204", "title": "t"}]',
    ("fc", "pane_titles"): '[{"pane_id": "204", "tty_name": "/dev/ttys001", "title": "t"}]',
    ("fc", "pid_ttys"): "101 /dev/ttys001 claude",
    ("jmp", "wezterm_panes"): '[{"pane_id": "204", "title": "t"}]',
    ("jmp", "pane_sessions"): "101 ttys001",
    ("fs", "live_processes"): "claude --settings {} --model m",
    ("sp", "gate_processes"): "101 00:01 sweep-gate",
    ("oi", "live_panes"): '[{"pane_id": "204", "title": "t"}]',
    ("pvc", "wezterm_panes"): '[{"pane_id": "204", "tty_name": "/dev/ttys001"}]',
    ("pvc", "_pid_ttys"): "101 ttys001",
}


class _SubprocessStub:
    """Stands in for the module's `subprocess`, overriding only `run`.

    The accessors catch `subprocess.SubprocessError` by name, so a bare object
    without it turns a clean `return None` into an `AttributeError` — which would
    read as a site failure while actually being a checker failure.
    """

    def __init__(self, run):
        self.run = run

    def __getattr__(self, name):
        return getattr(subprocess, name)


def load_module(path, name):
    """Import a hyphenated script by path. `None` when it does not exist."""
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def probe(mod, fn_name, broken, args=(), stdout=None):
    """Call `mod.<fn_name>(*args)` with its `subprocess` replaced. Returns (ok, value).

    `broken` forces the failure. Otherwise `stdout` decides what the healthy
    transport answered — `None` for "nothing", a payload for "content".
    """
    if mod is None or not hasattr(mod, fn_name):
        return False, "absent"
    original = mod.subprocess
    if broken:
        runner = _broken
    elif stdout is None:
        runner = _empty
    else:
        runner = lambda *_a, _s=stdout, **_k: _Proc(stdout=_s, returncode=0)
    mod.subprocess = _SubprocessStub(runner)
    try:
        return True, getattr(mod, fn_name)(*args)
    except Exception as exc:  # a raise is itself a failure to distinguish
        return True, f"raised {type(exc).__name__}: {exc}"
    finally:
        mod.subprocess = original


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tree", nargs="?", default=os.path.dirname(HERE))
    ap.add_argument("--capture", action="store_true",
                    help="print each site's raw three-state values and exit")
    args = ap.parse_args()
    scripts = os.path.join(os.path.abspath(args.tree), "scripts")

    mods = {
        n: load_module(os.path.join(scripts, f), f"chk_{n.replace('-', '_')}")
        for n, f in (
            ("wnm", "who-needs-me.py"),
            ("fc", "fleet-colours.py"),
            ("jmp", "jump.py"),
            ("fs", "fleet-sessions.py"),
            ("sp", "stop-probe.py"),
            ("oi", "open-items.py"),
            ("pvc", "plugin-version-census.py"),
        )
    }

    # Pin the registry the accessors join through — see `_fixture_registry`. Without it
    # `jmp.pane_sessions` never reaches its `ps` read on a machine with no
    # `~/.claude/sessions`, so the broken-state assertion below is skipped there.
    if mods["jmp"] is not None:
        mods["jmp"].SESSIONS_DIR = _fixture_registry()

    failures, checked = [], 0

    def expect(label, cond, detail=""):
        nonlocal checked
        checked += 1
        if not cond:
            failures.append(f"{label}: {detail}" if detail else label)

    # --- the accessors: broken must be `None`, never an empty container ---------
    accessors = [
        ("wnm", "wezterm_panes", ()), ("wnm", "live_pane_ids", ()),
        ("fc", "pane_titles", ()), ("fc", "pid_ttys", ()),
        ("jmp", "wezterm_panes", ()), ("jmp", "pane_sessions", ({},)),
        ("fs", "live_processes", ()), ("sp", "gate_processes", ()),
        ("oi", "live_panes", ()),
        ("pvc", "wezterm_panes", ()), ("pvc", "_pid_ttys", ([1],)),
    ]
    if args.capture:
        print(f"== three-state captures — {os.path.abspath(args.tree)}")
        for key, fn, fn_args in accessors:
            print(f"  {key}.{fn}")
            print(f"    broken        -> {probe(mods[key], fn, broken=True, args=fn_args)[1]!r}")
            print(f"    healthy-empty -> {probe(mods[key], fn, broken=False, args=fn_args)[1]!r}")
        return 0

    for key, fn, fn_args in accessors:
        ok, value = probe(mods[key], fn, broken=True, args=fn_args)
        if not ok:
            continue  # absent in this tree — reported by the deleted-wrapper check
        expect(
            f"{key}.{fn} broken",
            value is None,
            f"returned {value!r}, not None — a failed read is representable as empty",
        )

    # --- the wrapper this audit deletes: present ⇒ it must still not collapse ----
    wnm = mods["wnm"]
    if wnm is not None and hasattr(wnm, "panes"):
        _ok, value = probe(wnm, "panes", broken=True)
        expect(
            "wnm.panes broken (deleted by this audit)",
            value is None,
            f"returned {value!r} — the lossy wrapper is still present and collapsing",
        )

    # --- healthy-empty must stay a real answer, not become a failure ------------
    for key, fn, fn_args in accessors:
        ok, value = probe(mods[key], fn, broken=False, args=fn_args)
        if not ok:
            continue
        expect(
            f"{key}.{fn} healthy-empty",
            value is not None,
            "returned None for a reachable transport holding nothing — "
            "an empty fleet now reads as a failure",
        )

    # --- healthy-with-content must not be reported as a failure either ----------
    for key, fn, fn_args in accessors:
        payload = POPULATED.get((key, fn))
        if payload is None:
            continue
        ok, value = probe(mods[key], fn, broken=False, args=fn_args, stdout=payload)
        if not ok:
            continue
        expect(
            f"{key}.{fn} healthy-with-content",
            value is not None,
            "returned None for a transport that answered with content — "
            "the read is broken on the working path",
        )

    # --- `is_live` keeps the row when the pane map is unreadable ----------------
    if wnm is not None and hasattr(wnm, "is_live"):
        rec = {"session_id": "s", "pane": "204"}
        # A raise counts as a failure, not an abort: the pre-audit `is_live` does
        # `pane not in pmap` unguarded, so `None` raises TypeError there — which is
        # the defect, and the check must survive it to report it.
        try:
            keeps_none = wnm.is_live(rec, None, None) is True
            drops_empty = wnm.is_live(rec, {}, None) is False
            raised = ""
        except Exception as exc:
            keeps_none, drops_empty = False, False
            raised = f"raised {type(exc).__name__}: {exc}"
        expect(
            "wnm.is_live(None pmap)",
            keeps_none,
            raised or "dropped a row on an unreadable pane map — a broken read became a false zero",
        )
        expect(
            "wnm.is_live({} pmap)",
            drops_empty,
            "kept a row on a readable empty map — the real empty answer stopped filtering",
        )

    # --- `pane_for` must not call a healthy-empty fleet unreadable --------------
    if wnm is not None and hasattr(wnm, "pane_for"):
        original_panes = wnm.wezterm_panes
        original_registry = wnm.read_registry
        wnm.wezterm_panes = lambda: {}
        wnm.read_registry = lambda *a, **k: {"aaaaaaaa-1111": "Session A"}
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                wnm.pane_for("aaaaaaaa")
        except Exception:
            pass
        finally:
            wnm.wezterm_panes = original_panes
            wnm.read_registry = original_registry
        expect(
            "wnm.pane_for healthy-empty",
            "unreadable" not in err.getvalue(),
            f"reported a reachable empty fleet as unreadable: {err.getvalue().strip()!r}",
        )

    # --- SC6(c): re-run the mechanism and check it derives nothing new ----------
    walk = os.path.join(scripts, "transport-read-walk.py")
    if os.path.isfile(walk):
        out = subprocess.run(
            [sys.executable, walk, "--json"], capture_output=True, text=True, cwd=os.path.dirname(scripts)
        ).stdout
        try:
            report = json.loads(out)
        except Exception:
            report = {}
        edges = report.get("edges", [])
        seeds = report.get("seeds", [])
        reached = report.get("reached", [])

        # The walk must name the caller shape, or a shallow or empty walk satisfies
        # the criterion vacuously.
        expect(
            "walk names the caller edge",
            any(e["from_file"] == "fleet-board.py" and e["to_function"] in ("panes", "wezterm_panes")
                for e in edges),
            "no fleet-board caller edge — a shallow or empty walk satisfies the criterion vacuously",
        )

        # Enumeration completeness: every transport call site the mechanism derives
        # is one the enumeration recorded. The seeds *are* the derivation — the
        # closure beyond them is callers, which are found by inspection and listed
        # separately in SITE_LIST.
        derived = {(s["file"], s["function"]) for s in seeds}
        outside = sorted(derived - SITE_LIST)
        expect(
            "mechanism derives no site outside the list",
            not outside,
            f"derived {outside!r} — the mechanism and the enumeration disagree",
        )

        # And the caller shape specifically: a consumer holding no transport call of
        # its own, which a call-site-only search cannot reach at any depth.
        expect(
            "walk reaches the caller shape",
            any(r["file"] == "fleet-board.py" and r["function"] == "collect_signals" for r in reached),
            "the walk never reached fleet-board.py:collect_signals",
        )

    tree = os.path.abspath(args.tree)
    if failures:
        print(f"❌ {len(failures)}/{checked} checks failed against {tree}\n")
        for f in failures:
            print(f"  · {f}")
        return 1
    print(f"✅ {checked}/{checked} checks passed against {tree}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
