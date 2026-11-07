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
(`completed`) is set back to `phase: execution`, its `completed_date`
cleared, and `status: in_progress` written last. An `aborted` row is the
operator's rejection and is reported, never reopened. An entry with
`"enabled": false` reads OFF and exits 0 — a service stopped on purpose. An open row is left
untouched — it is already open.

Exit codes: 0 — every service measured (OK or BREACH) and every reopen
succeeded; 1 — any UNKNOWN reading, any REOPEN FAILED, or a REFUSED
(unreadable or malformed) config.
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
# Only `completed` is reopened. ⚠️ `aborted` is the operator's REJECTION — the
# inbox documents it as irreversible — and an aborted row that was never
# approved carries no `approved_at`, which the open leg requires, so a reopened
# aborted row would sit `in_progress` with no session forever. A breach on an
# aborted row is reported and left alone.
REOPENABLE_STATUSES = ("completed",)

Runner = Callable[[list], str]


def run(cmd: list) -> str:
    """Run cmd and return stdout; raise on a non-zero exit."""
    return subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30).stdout


_UNITS = {"B": 1 / (1024 * 1024), "KB": 1 / 1024, "MB": 1.0, "GB": 1024.0}


def parse_footprint_mb(text: str) -> Optional[float]:
    """Return phys_footprint in MB from `footprint -p` output, or None."""
    m = re.search(r"phys_footprint:\s*([\d,.]+)\s*(B|KB|MB|GB)\b", text)
    if not m:
        return None
    # A grouped value (`5,522 MB`) must parse, not read UNKNOWN forever.
    return float(m.group(1).replace(",", "")) * _UNITS[m.group(2)]


def parse_launchctl_pid(text: str) -> Optional[int]:
    """Return the PID from `launchctl list <label>` output, or None if not running."""
    m = re.search(r'"PID"\s*=\s*(\d+);', text)
    return int(m.group(1)) if m else None


@dataclass
class Reading:
    label: str
    state: str  # OK | BREACH | UNKNOWN | OFF
    pid: Optional[int] = None
    footprint_mb: Optional[float] = None
    cpu_pct: Optional[float] = None
    reason: str = ""

    def line(self) -> str:
        if self.state == "OFF":
            return f"service-guard: {self.label} OFF — {self.reason}"
        if self.state == "UNKNOWN":
            return f"service-guard: {self.label} UNKNOWN — {self.reason}"
        cpu = f" · cpu {self.cpu_pct:.1f}%" if self.cpu_pct is not None else ""
        tail = f" — {self.reason}" if self.reason else ""
        return (f"service-guard: {self.label} {self.state} · pid {self.pid}"
                f" · footprint {self.footprint_mb:.0f} MB{cpu}{tail}")


def measure(service: dict, runner: Runner) -> Reading:
    label = service["label"]
    # ⚠️ OFF is a deliberate state, not a failure: a service stopped on purpose
    # must not read UNKNOWN (exit 1) every round — an always-red guard trains
    # the operator to ignore it. UNKNOWN stays reserved for "could not measure"
    # on a service that should be running.
    if service.get("enabled") is False:
        return Reading(label, "OFF", reason="disabled in config")
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


REQUIRED_KEYS = ("label", "max_footprint_mb", "task", "vault")


class ReopenFailed(Exception):
    """The owning row could not be read or reopened."""


def validate(services) -> Optional[str]:
    """Return a refusal for a malformed config, or None when every entry is usable."""
    if not isinstance(services, list):
        return "config must be a JSON list of service entries"
    for i, entry in enumerate(services):
        if not isinstance(entry, dict):
            return f"entry {i} is not an object"
        missing = [k for k in REQUIRED_KEYS if k not in entry]
        if missing:
            return f"entry {i} missing {', '.join(missing)}"
        for k in ("label", "task", "vault"):
            value = entry[k]
            if not isinstance(value, str) or not value.strip():
                return f"entry {i} {k} must be a non-empty string"
            # ⚠️ These reach `launchctl` / `vault-cli` argv. A leading dash would
            # be parsed as a flag rather than as data.
            if value.startswith("-"):
                return f"entry {i} {k} must not start with '-'"
        if "enabled" in entry and not isinstance(entry["enabled"], bool):
            return f"entry {i} enabled must be true or false"
        for k in ("max_footprint_mb", "max_cpu_pct"):
            if k not in entry:
                continue
            value = entry[k]
            # ⚠️ `bool` is a subclass of `int`: `true` would pass as a 1 MB bar.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return f"entry {i} {k} is not a number"
            # A bar of zero or below makes every healthy reading a BREACH and
            # reopens the owning row on a service that is fine.
            if value <= 0:
                return f"entry {i} {k} must be greater than 0"
    return None


def task_status(base: list, task: str, runner: Runner) -> str:
    """Read the owning row's status. `--output json` exits 0 on an unknown task
    and carries the error in the payload, so a missing `value` is a failure —
    never an "already open" row."""
    try:
        payload = json.loads(runner(base + ["get", task, "status", "--output", "json"]))
    except (ValueError, TypeError) as exc:
        raise ReopenFailed(f"unparseable status read: {exc}") from exc
    value = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(value, str) or not value:
        error = payload.get("error") if isinstance(payload, dict) else payload
        raise ReopenFailed(f"status read failed: {error}")
    return value


def reopen(service: dict, runner: Runner) -> str:
    """Reopen the owning task when it is terminal. Returns a one-line outcome.

    Uses the canonical reopen — phase, clear `completed_date`, then status.
    ⚠️ **The status write goes LAST**, because it is the one that ends the
    retry: a row is reopened only while it reads terminal. With status last, any
    earlier failure leaves the row terminal and the next round repeats the whole
    reopen; with it anywhere earlier, a later failure would leave a non-terminal
    row the next round skips as "already open", never repaired.
    """
    base = ["vault-cli", "--vault", service["vault"], "task"]
    task = service["task"]
    status = task_status(base, task, runner)
    if status == "aborted":
        return (f"service-guard: {service['label']} owning row is aborted — "
                f"not reopened (an operator rejection stands)")
    if status not in REOPENABLE_STATUSES:
        return f"service-guard: {service['label']} owning row already open (status: {status})"
    try:
        runner(base + ["set", task, "phase", "execution"])
        runner(base + ["clear", task, "completed_date"])
        runner(base + ["set", task, "status", "in_progress"])
    except Exception as exc:  # noqa: BLE001 — surfaced as REOPEN FAILED
        raise ReopenFailed(f"reopen write failed: {exc}") from exc
    return f"service-guard: {service['label']} REOPENED \"{task}\" (was {status})"


def main(argv: Optional[list] = None, runner: Runner = run) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--apply", action="store_true", help="reopen the owning task on a breach")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    def early_exit(line: str, error: Optional[str], code: int) -> int:
        # ⚠️ `--json` must hold on EVERY exit, not only the measured one — a JSON
        # caller needs the structured answer exactly when the config is wrong.
        if args.json:
            print(json.dumps({"readings": [], "outcomes": [], "error": error}))
        else:
            print(line)
        return code

    if not os.path.exists(args.config):
        return early_exit("service-guard: no config", None, 0)
    try:
        with open(args.config, encoding="utf-8") as fh:
            services = json.load(fh)
    except (OSError, ValueError) as exc:
        reason = f"unreadable config {args.config}: {exc}"
        return early_exit(f"service-guard: REFUSED — {reason}", reason, 1)
    refusal = validate(services)
    if refusal:
        reason = f"{refusal} ({args.config})"
        return early_exit(f"service-guard: REFUSED — {reason}", reason, 1)

    readings = [measure(s, runner) for s in services]
    outcomes = []
    reopen_failed = False
    if args.apply:
        for service, reading in zip(services, readings):
            if reading.state == "BREACH":
                try:
                    outcomes.append(reopen(service, runner))
                except Exception as exc:  # noqa: BLE001 — any failure is a REOPEN FAILED line
                    reopen_failed = True
                    outcomes.append(f"service-guard: {service['label']} REOPEN FAILED — {exc}")

    if args.json:
        print(json.dumps({"readings": [r.__dict__ for r in readings], "outcomes": outcomes}))
    else:
        for r in readings:
            print(r.line())
        for o in outcomes:
            print(o)
    # A structured flag, never a substring scan: an outcome line echoes the
    # task title, and a title containing "FAILED" must not fail a clean reopen.
    failed = reopen_failed or any(r.state == "UNKNOWN" for r in readings)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
