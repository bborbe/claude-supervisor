#!/usr/bin/env python3
"""Reproduce the swallowed-submit drop, and prove it is the pane's state, not the text.

The defect: `send_agent_message` returns `{sent: true}` whenever `send-text` reaches the
pty, whether or not the Enter submitted. A pane that is mid-turn still shows an EMPTY
composer, so `waitUntilReady` calls it ready, the keystrokes land, and the TUI discards
the Enter while it is still processing.

This drill produces the two transcripts the task requires:
  BUSY  — text typed while the pane is mid-turn: expected to strand in the composer
  IDLE  — the same text typed once the pane is idle: expected to submit

Both runs read the pane back with `get-text`, so the verdict is the pane's own state
rather than the send's exit code — the exact confusion this defect is made of.

Usage:
    send-delivery-drill.py --pane-id 940
    send-delivery-drill.py --pane-id 940 --busy-wait 2.0

⚠️ It drives a pane the caller already owns. It never spawns one: opening a session is
a fleet-level act, so the pane must exist before this runs.
"""
import argparse, json, subprocess, sys, time

MARKER = "SEND-DELIVERY-DRILL"


def wezterm(args):
    return subprocess.run(["wezterm", *args], capture_output=True, text=True)


def pane_text(pane_id, scrollback=200):
    res = wezterm(["cli", "get-text", "--pane-id", str(pane_id), "--start-line", str(-scrollback)])
    return res.stdout if res.returncode == 0 else None


def composer(pane_id):
    """The last line drawn with the prompt glyph — the same rule as server/tab.mjs."""
    text = pane_text(pane_id)
    if text is None:
        return None
    for line in reversed(text.splitlines()):
        if line.startswith("❯"):
            return line[1:].strip()
    return None


def pane_exists(pane_id):
    res = wezterm(["cli", "list", "--format", "json"])
    if res.returncode != 0:
        return False
    try:
        return any(str(p.get("pane_id")) == str(pane_id) for p in json.loads(res.stdout))
    except Exception:
        return False


def type_and_enter(pane_id, text):
    """What sendToPane does: frame the message with \\r and push it in one write."""
    wezterm(["cli", "activate-pane", "--pane-id", str(pane_id)])
    return wezterm(["cli", "send-text", "--pane-id", str(pane_id), "--no-paste", f"{text}\r"])


def observe(pane_id, seconds):
    """Watch for the submit: the composer clears and the text leaves the input box."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        c = composer(pane_id)
        if c is None:
            return {"composer": None, "cleared": None}
        if c == "":
            return {"composer": c, "cleared": True}
        time.sleep(0.25)
    return {"composer": composer(pane_id), "cleared": False}


def run(pane_id, label, settle):
    if settle:
        print(f"   waiting {settle}s for the pane to be mid-turn...")
        time.sleep(settle)
    before = pane_text(pane_id)
    text = f"{MARKER} {label}"
    res = type_and_enter(pane_id, text)
    print(f"   send-text exit: {res.returncode} (0 = keystrokes reached the pty)")
    seen = observe(pane_id, 6)
    after = pane_text(pane_id)
    stranded = bool(seen["composer"]) and MARKER in (seen["composer"] or "")
    return {
        "label": label,
        "exit_code": res.returncode,
        "composer_after": seen["composer"],
        "cleared": seen["cleared"],
        "stranded": stranded,
        "text_visible": bool(after) and text in after,
        "before_tail": (before or "").splitlines()[-3:],
        "after_tail": (after or "").splitlines()[-3:],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pane-id", required=True)
    ap.add_argument("--busy-wait", type=float, default=0.5,
                    help="seconds to sleep after typing, to land the Enter mid-turn")
    ap.add_argument("--out", default=None, help="write both transcripts here as JSON")
    args = ap.parse_args()

    if not pane_exists(args.pane_id):
        print(f"FAIL: pane {args.pane_id} does not exist — an unreadable pane is not a clean one")
        return 2

    results = []
    print(f"pane {args.pane_id}: BUSY run (Enter raced against an in-flight turn)")
    results.append(run(args.pane_id, "busy", args.busy_wait))

    print(f"pane {args.pane_id}: IDLE run (same text, pane settled)")
    results.append(run(args.pane_id, "idle", 0))

    print()
    for r in results:
        print(f"[{r['label']}] exit={r['exit_code']} cleared={r['cleared']} "
              f"stranded={r['stranded']} text_visible={r['text_visible']}")
        print(f"   composer after: {r['composer_after']!r}")

    busy, idle = results
    reproduced = busy["exit_code"] == 0 and busy["stranded"]
    control_ok = idle["exit_code"] == 0 and idle["cleared"] and not idle["stranded"]
    print()
    if reproduced and control_ok:
        print("RESULT: REPRODUCED — busy run returned exit 0 with the text still in the "
              "composer; idle run submitted. The condition is the pane's turn state.")
    elif reproduced:
        print("RESULT: PARTIAL — busy run stranded, but the idle control did not submit; "
              "the run is not yet evidence (a probe that cannot pass proves nothing).")
    else:
        print("RESULT: NOT REPRODUCED — the busy run did not strand. Do not record this "
              "as evidence; raise --busy-wait and retry.")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2)
        print(f"transcripts: {args.out}")

    return 0 if (reproduced and control_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
