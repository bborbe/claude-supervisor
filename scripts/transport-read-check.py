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

HERE = os.path.dirname(os.path.abspath(__file__))


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


def probe(mod, fn_name, broken, args=()):
    """Call `mod.<fn_name>(*args)` with its `subprocess` replaced. Returns (ok, value)."""
    if mod is None or not hasattr(mod, fn_name):
        return False, "absent"
    original = mod.subprocess
    mod.subprocess = _SubprocessStub(_broken if broken else _empty)
    try:
        return True, getattr(mod, fn_name)(*args)
    except Exception as exc:  # a raise is itself a failure to distinguish
        return True, f"raised {type(exc).__name__}: {exc}"
    finally:
        mod.subprocess = original


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tree", nargs="?", default=os.path.dirname(HERE))
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
        )
    }

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
    ]
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

    # --- the walk's edge list still names the caller shape (SC6(c)) -------------
    walk = os.path.join(scripts, "transport-read-walk.py")
    if os.path.isfile(walk):
        out = subprocess.run(
            [sys.executable, walk, "--json"], capture_output=True, text=True, cwd=os.path.dirname(scripts)
        ).stdout
        try:
            edges = json.loads(out)["edges"]
        except Exception:
            edges = []
        expect(
            "walk names the caller edge",
            any(e["from_file"] == "fleet-board.py" and e["to_function"] == "panes" for e in edges)
            or any(e["from_file"] == "fleet-board.py" and e["to_function"] == "wezterm_panes" for e in edges),
            "no fleet-board caller edge — a shallow or empty walk satisfies the criterion vacuously",
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
