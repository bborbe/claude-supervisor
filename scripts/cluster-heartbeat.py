#!/usr/bin/env python3
"""Read the cluster heartbeat store — the cluster half of `session-liveness.py`.

A cluster worker runs as a pod on another machine. It holds no entry in the local registry
(that directory is pid-keyed and local-only), and no process of its own on this host, so every
probe a local reader can run finds nothing — and finds the SAME nothing for a worker mid-turn
and for one that finished an hour ago. That is the defect: the readers act on "not live" by
allowing a resume, so two writers land on one conversation.

The cluster worker publishes instead. While it is live it refreshes its own entry in a
ConfigMap; a local mirror folds a fresh entry into the local heartbeat store, where
`live-workers.py` already knows how to read it. This file is the cluster store's **single
reader** — one instrument per store, the same rule that keeps `live-workers.py` the only
reader of the local heartbeat dir.

  --list                 one line per live cluster worker: `<session-id>  age <n>s`
  --check <session-id>   LIVE (exit 0) / STALE (exit 1) / UNKNOWN (exit 2)

⚠️ **UNKNOWN is a third answer, not a flavour of STALE.** A cluster that cannot be reached
means the probe could not run, and a caller that folds that into "not live" turns a network
fault into permission to start a second writer. Exit 2 is deliberately distinct from exit 1,
and it is the same rule `session-liveness.py` and `live-workers.py` already carry.

⚠️ **An ABSENT ConfigMap is not an unreachable cluster, and the two must not collapse.** The
read uses `--ignore-not-found`, so a missing ConfigMap exits 0 with empty output — "no cluster
worker is registered", a readable-and-empty answer. A non-zero exit is the cluster failing to
answer at all, and only that is UNKNOWN. Without the flag both would be a non-zero exit and
the distinction would be unrecoverable.

⚠️ **The verdict is the stamp's age, never the key's existence.** A pod killed with no chance
to clean up leaves its entry behind; an entry that stopped being refreshed is a DEAD worker,
and reading existence reports exactly the wrong answer for the case this store exists to
catch.

⚠️ **The age comes from the record's `refreshedAt`, not from a Kubernetes timestamp.** A
ConfigMap has no mtime, so there is nothing on the object that moves when its data is
rewritten — `metadata.creationTimestamp` is fixed at creation and would make every worker look
older the longer it lived. The writer therefore stamps `refreshedAt` itself. That is the one
place this file departs from `server/heartbeat.mjs`'s "mtime is the authority, never a field
inside it" rule, and it is a departure forced by the store, not a preference: the rule's
purpose is that the age cannot disagree with the fact that a write happened, and on a
ConfigMap the writer's own timestamp is the only thing that does. The local mirror keeps the
stronger property on this side of the boundary — it refreshes a local stamp whose **mtime**
is the verdict's authority — so the mtime rule holds everywhere it can.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

LIVE, STALE, UNKNOWN = 0, 1, 2

# Mirrors HEARTBEAT_TTL_MS in server/heartbeat.mjs, like live-workers.py's copy: this script
# must run without a Node toolchain, so it cannot import the constant.
TTL_SECONDS = 60

# The house cluster wrappers. `kubectlnukedev` is the dev-cluster wrapper on this machine;
# the plugin never uses `--context` (see the operator's cluster rules), so the wrapper name IS
# the cluster selection and it is configuration, not a literal.
DEFAULT_CMD = "kubectlnukedev"
DEFAULT_NAMESPACE = "dev"
DEFAULT_CONFIGMAP = "claude-worker-heartbeats"


def cluster_cmd():
    return os.environ.get("SUPERVISOR_CLUSTER_CMD") or DEFAULT_CMD


def cluster_namespace():
    return os.environ.get("SUPERVISOR_CLUSTER_NAMESPACE") or DEFAULT_NAMESPACE


def cluster_configmap():
    return os.environ.get("SUPERVISOR_CLUSTER_CONFIGMAP") or DEFAULT_CONFIGMAP


def read_store(cmd=None, namespace=None, configmap=None, ttl=TTL_SECONDS, now=None, runner=None):
    """Fresh cluster stamps as `[{session_id, age_seconds, refreshed_at}]`, or `None`.

    `None` and `[]` are different answers and must stay so: `[]` is "the cluster answered, no
    worker is registered", `None` is "the cluster did not answer". Collapsing them is how a
    network fault becomes a confident all-clear — the same rule `live-workers.py` states for
    its own store.
    """
    now = time.time() if now is None else now
    exe = cmd if cmd is not None else cluster_cmd()
    if shutil.which(exe) is None and runner is None:
        return None
    argv = [
        exe,
        "get",
        "configmap",
        configmap if configmap is not None else cluster_configmap(),
        "-n",
        namespace if namespace is not None else cluster_namespace(),
        "-o",
        "json",
        # See the module docstring: without this, "no ConfigMap yet" and "cluster unreachable"
        # are both a non-zero exit, and the distinction SC4 rests on becomes unrecoverable.
        "--ignore-not-found",
    ]
    run = runner or (lambda a: subprocess.run(a, capture_output=True, text=True, timeout=15))
    try:
        proc = run(argv)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    body = (proc.stdout or "").strip()
    if not body:
        return []  # `--ignore-not-found` on a missing ConfigMap: readable, and empty
    try:
        obj = json.loads(body)
    except ValueError:
        return None
    data = (obj or {}).get("data") or {}

    out = []
    for session_id, raw in data.items():
        try:
            record = json.loads(raw)
        except (TypeError, ValueError):
            continue
        refreshed = record.get("refreshedAt")
        try:
            at = _parse_rfc3339(refreshed)
        except (TypeError, ValueError):
            continue
        age = now - at
        if age >= ttl or age < 0:
            continue
        out.append({"session_id": session_id, "age_seconds": round(age, 1), "refreshed_at": refreshed})
    return out


def _parse_rfc3339(value):
    """Epoch seconds from an RFC3339 timestamp. Raises on anything else."""
    import datetime

    if not isinstance(value, str) or not value:
        raise ValueError("missing refreshedAt")
    text = value.strip().replace("Z", "+00:00")
    return datetime.datetime.fromisoformat(text).timestamp()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read the cluster heartbeat store.")
    parser.add_argument("--list", action="store_true", help="print every live cluster worker")
    parser.add_argument("--check", metavar="SESSION_ID", help="full id or an 8-char prefix")
    parser.add_argument("--ttl", type=int, default=TTL_SECONDS, help=f"staleness bound in seconds (default {TTL_SECONDS})")
    parser.add_argument("--cmd", default=None, help="override the cluster wrapper (tests pass a fake)")
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--configmap", default=None)
    args = parser.parse_args(argv)

    stamps = read_store(cmd=args.cmd, namespace=args.namespace, configmap=args.configmap, ttl=args.ttl)

    if stamps is None:
        print(
            "UNKNOWN — cluster store unreadable (%s -n %s)" % (args.cmd or cluster_cmd(), args.namespace or cluster_namespace()),
            file=sys.stderr,
        )
        return UNKNOWN

    if args.check:
        want = args.check.strip().lower()
        matches = [s for s in stamps if s["session_id"].lower().startswith(want)]
        if not matches:
            print("STALE — no fresh cluster stamp matches %s" % want, file=sys.stderr)
            return STALE
        if len(matches) > 1:
            print(
                "STALE — %d cluster stamps match %s, none uniquely: %s"
                % (len(matches), want, ", ".join(m["session_id"][:12] for m in matches)),
                file=sys.stderr,
            )
            return STALE
        print("LIVE — %s  age %ss" % (matches[0]["session_id"], matches[0]["age_seconds"]))
        return LIVE

    for stamp in sorted(stamps, key=lambda s: s["age_seconds"]):
        print("%s  age %ss" % (stamp["session_id"], stamp["age_seconds"]))
    if not stamps:
        print("no live cluster workers in %s/%s" % (args.namespace or cluster_namespace(), args.configmap or cluster_configmap()))
    return LIVE


if __name__ == "__main__":
    sys.exit(main())
