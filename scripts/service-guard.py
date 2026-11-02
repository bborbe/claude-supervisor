#!/usr/bin/env python3
"""Standing resource guard for long-running local services.

Each fleet round reads every guarded service's memory footprint and CPU,
compares them to the service's bar, and on a breach reopens the task row that
owns the service — so the next drift is caught by the system instead of by
the operator reading Activity Monitor.

Measure: `footprint -p <pid>` → `phys_footprint`, never `ps -o rss`. On macOS
the two diverge by more than an order of magnitude (measured 2026-11-02: one
process read 18 MB RSS against 5,522 MB footprint), and every earlier reading
that used RSS was wrong. CPU is `ps -o pcpu=`, the kernel's decaying recent
average — the same instrument the owning tasks' bars are written against.

Config (JSON, default ~/.claude/state/service-guard.json):

    [{"label": "com.bborbe.attention-controller",
      "max_footprint_mb": 150, "max_cpu_pct": 5,
      "task": "<owning task title>", "vault": "private-personal"}]

`max_cpu_pct` is optional. A missing config prints `service-guard: no config`
and exits 0 — an unguarded machine is a statement, not an error.

Output: one line per service, then exit 0 when every reading succeeded (with
or without a breach) and 1 when any reading failed. `unknown` is a third
answer, never rounded to `ok`: a service whose pid or footprint cannot be read
prints `UNKNOWN`, because "within bounds" and "I could not measure" must not
render the same.

With `--apply`, a breach reopens the owning task: a terminal row
(`completed` / `aborted`) is set back to `status: in_progress` and
`phase: execution`. An open row is left untouched — it is already open.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Optional

DEFAULT_CONFIG = os.path.expanduser("~/.claude/state/service-guard.json")
TERMINAL_STATUSES = ("completed", "aborted")

Runner = Callable[[list], str]


def run(cmd: list) -> str:
    """Run cmd and return stdout; raise on a non-zero exit."""
    return subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30).stdout


_UNITS = {"B": 1 / (1024 * 1024), "KB": 1 / 1024, "MB": 1.0, "GB": 1024.0}


def parse_footprint_mb(text: str) -> Optional[float]:
    """Return phys_footprint in MB from `footprint -p` output, or None."""
    m = re.search(r"phys_footprint:\s*([\d.]+)\s*(B|KB|MB|GB)\b", text)
    if not m:
        return None
    return float(m.group(1)) * _UNITS[m.group(2)]


def parse_launchctl_pid(text: str) -> Optional[int]:
    """Return the PID from `launchctl list <label>` output, or None if not running."""
    m = re.search(r'"PID"\s*=\s*(\d+);', text)
    return int(m.group(1)) if m else None


@dataclass
class Reading:
    label: str
    state: str  # OK | BREACH | UNKNOWN
    pid: Optional[int] = None
    footprint_mb: Optional[float] = None
    cpu_pct: Optional[float] = None
    reason: str = ""

    def line(self) -> str:
        if self.state == "UNKNOWN":
            return f"service-guard: {self.label} UNKNOWN — {self.reason}"
        cpu = f" · cpu {self.cpu_pct:.1f}%" if self.cpu_pct is not None else ""
        tail = f" — {self.reason}" if self.reason else ""
        return (f"service-guard: {self.label} {self.state} · pid {self.pid}"
                f" · footprint {self.footprint_mb:.0f} MB{cpu}{tail}")


def measure(service: dict, runner: Runner) -> Reading:
    label = service["label"]
    try:
        pid = parse_launchctl_pid(runner(["launchctl", "list", label]))
    except Exception as exc:  # noqa: BLE001 — every failure is an UNKNOWN reading
        return Reading(label, "UNKNOWN", reason=f"launchctl list failed: {exc}")
    if pid is None:
        return Reading(label, "UNKNOWN", reason="not running (no PID)")
    try:
        fp = parse_footprint_mb(runner(["footprint", "-p", str(pid)]))
    except Exception as exc:  # noqa: BLE001
        return Reading(label, "UNKNOWN", pid=pid, reason=f"footprint failed: {exc}")
    if fp is None:
        return Reading(label, "UNKNOWN", pid=pid, reason="no phys_footprint line")
    cpu = None
    if service.get("max_cpu_pct") is not None:
        try:
            cpu = float(runner(["ps", "-o", "pcpu=", "-p", str(pid)]).strip())
        except Exception as exc:  # noqa: BLE001
            return Reading(label, "UNKNOWN", pid=pid, footprint_mb=fp, reason=f"ps failed: {exc}")
    over = []
    if fp > service["max_footprint_mb"]:
        over.append(f"footprint {fp:.0f} MB > {service['max_footprint_mb']} MB")
    if cpu is not None and cpu > service["max_cpu_pct"]:
        over.append(f"cpu {cpu:.1f}% > {service['max_cpu_pct']}%")
    state = "BREACH" if over else "OK"
    return Reading(label, state, pid=pid, footprint_mb=fp, cpu_pct=cpu, reason="; ".join(over))


def reopen(service: dict, runner: Runner) -> str:
    """Reopen the owning task when it is terminal. Returns a one-line outcome."""
    base = ["vault-cli", "--vault", service["vault"], "task"]
    task = service["task"]
    status = runner(base + ["get", task, "status"]).strip()
    if status not in TERMINAL_STATUSES:
        return f"service-guard: {service['label']} owning row already open (status: {status})"
    runner(base + ["set", task, "status", "in_progress"])
    runner(base + ["set", task, "phase", "execution"])
    return f"service-guard: {service['label']} REOPENED \"{task}\" (was {status})"


def main(argv: Optional[list] = None, runner: Runner = run) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--apply", action="store_true", help="reopen the owning task on a breach")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if not os.path.exists(args.config):
        print("service-guard: no config")
        return 0
    with open(args.config, encoding="utf-8") as fh:
        services = json.load(fh)

    readings = [measure(s, runner) for s in services]
    outcomes = []
    if args.apply:
        for service, reading in zip(services, readings):
            if reading.state == "BREACH":
                try:
                    outcomes.append(reopen(service, runner))
                except Exception as exc:  # noqa: BLE001
                    outcomes.append(f"service-guard: {service['label']} REOPEN FAILED — {exc}")

    if args.json:
        print(json.dumps({"readings": [r.__dict__ for r in readings], "outcomes": outcomes}))
    else:
        for r in readings:
            print(r.line())
        for o in outcomes:
            print(o)
    failed = any(r.state == "UNKNOWN" for r in readings) or any("FAILED" in o for o in outcomes)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
