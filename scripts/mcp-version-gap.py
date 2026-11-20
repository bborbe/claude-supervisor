#!/usr/bin/env python3
"""mcp-version-gap — does the MCP server's running binary match a released tag?

The supervisor MCP server is served by a **compiled binary**, `~/.local/bin/claude-wrapper`,
on `127.0.0.1:18090/supervisor/mcp`. Every policy decision a session makes comes from that
binary, and **nothing checked whether it matched a release**: measured 2026-11-20 the running
build was `v0.45.2-0.20261120032107-c84c80e5ee16` — a Go **pseudo-version**, i.e. built from a
commit carrying no release tag — while `bborbe/claude-wrapper` had released **`v0.48.0`** that
morning. Three releases behind, and a pseudo-version is by definition not any release at all.

**The binary is resolved from the port it listens on, never from a config guess.** The MCP
entry in `~/.claude/mcp-*.json` is `{"type": "http", "url": "http://127.0.0.1:18090/…"}` — a
URL, not a command, so the config names no program. `lsof -nP -iTCP:<port> -sTCP:LISTEN`
names the pid that actually serves it, `ps -o comm= -p <pid>` names the path, and
`go version -m <path>` names what that path was built from. A probe reading `git describe` in
a checkout answers a different question — what the tree says — and never touches what the
process loaded.

**Three states, and `unread` is never folded into either.** `agreement` / `divergent` /
`unread`. A failed `lsof`, an unreadable binary, a missing `go`, an unreachable `gh` — each is
`unread` with its own error, never `agreement` (a clean answer derived from a probe that could
not read its own reference) and never `divergent` (a defect claimed from a read that did not
happen). Exit codes follow: `0` agreement, `1` divergent, `2` unread.

⚠️ **A probe that always reports divergence fails exactly as one that always reports
agreement.** The two branches are the same comparison over two inputs; `--running-version` and
`--released-tag` feed either side so the agreement branch is provable without pinning a tag in
the tree live sessions execute (that tree is the running server's own checkout, and moving it
is a different, production-touching change). Every override is recorded under `overrides`, so a
fed value is never mistaken for a measured one.

⚠️ **The gap is counted from the release list, not asserted.** It is the number of releases
strictly newer than the running version's base — so a pseudo-version from `v0.45.2` against a
list topping at `v0.48.0` reads `gap: 3`, the same number a human gets counting the tags. When
the base is not in the fetched window the gap is `null` and both versions are still quoted: an
unknown distance is not an agreement.

  (default)     human report — a named state, both versions, and the gap
  --line        ONE line, for a round's own render (`commands/steps/round-readings.md` step 3)
  --json        the same reading as JSON, for `scripts/round-readings.py` and its payload
  --post        post an `ack` card on the attention board when the state is `divergent`,
                deduped on the version pair so one divergence is one card, not one per tick

Diagnostics, all of which still name the binary they resolved: `--pid`, `--binary`,
`--build-info <file>` (a `go version -m` dump), `--releases-json <file>` (a `gh release list`
dump), `--running-version V`, `--released-tag T`.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ASK = os.path.join(HERE, "attention-ask.py")

DEFAULT_PORT = 18090
DEFAULT_REPO = "bborbe/claude-wrapper"

AGREEMENT = "agreement"
DIVERGENT = "divergent"
UNREAD = "unread"

# A released tag is exactly `vX.Y.Z`. Anything else — a pseudo-version, `(devel)` — is not a
# release, and the comparison must say so rather than lexically comparing two strings that
# were never the same kind of thing.
RELEASE_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
# `vX.Y.Z-pre.0.<14-digit timestamp>-<12 hex>` — Go's pseudo-version form. The base is the
# release the commit sits after, which is what the gap is counted from.
PSEUDO_RE = re.compile(r"^v(\d+\.\d+\.\d+)-.+$")

# `gh release list` prints a fixed number of entries; the gap is only countable inside the
# window that was fetched, so the window is generous and its size is recorded with the reading.
RELEASE_WINDOW = 200

# Bounded so a hung `gh` cannot hold a round open. The round's own cadence is minutes; this is
# a network read, not a long poll.
GH_TIMEOUT = 30
CMD_TIMEOUT = 15


def _run(cmd, timeout=CMD_TIMEOUT):
    """(stdout, error) — an OSError or a non-zero exit is an error, never empty output."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return None, f"{cmd[0]}: timed out after {timeout}s"
    except OSError as error:
        return None, f"{cmd[0]}: {error}"
    if proc.returncode != 0:
        detail = (proc.stderr.strip().splitlines() or ["(no output)"])[-1]
        return None, f"{cmd[0]} exited {proc.returncode}: {detail}"
    return proc.stdout, None


def parse_lsof_listeners(text):
    """The pids listening, from `lsof -nP -iTCP:<port> -sTCP:LISTEN` output, in order.

    ⚠️ **The pid is the SECOND field, and the header line is skipped by shape rather than by
    position.** `awk 'NR==2{print $2}'` works only because a one-pid listener emits exactly one
    row; a dual-stack listener emits two rows for one pid, and a second process sharing the port
    emits two pids. Reading the whole output and requiring a numeric second field keeps both
    cases answerable instead of silently taking whichever row happened to be second.
    """
    pids = []
    for line in str(text).splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1].isdigit():
            pid = int(fields[1])
            if pid not in pids:
                pids.append(pid)
    return pids


def resolve_pid(port, lsof=None):
    """(pids, error) — the pids listening on `port`, resolved from the port itself.

    ⚠️ **The empty answer is an error on BOTH paths.** An injected read that parsed to no pid
    is the same fact as a live `lsof` that answered with nothing: nothing is listening. Only
    the live path can also fail to *run*, and that failure is named separately.
    """
    if lsof is None:
        out, error = _run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"])
        if error:
            return [], f"could not resolve the listener on port {port}: {error}"
    else:
        out = lsof
    pids = parse_lsof_listeners(out)
    if not pids:
        return [], f"nothing listens on port {port}"
    return pids, None


def resolve_binary(pid, ps=None):
    """(path, error) — the executable `pid` is running.

    ⚠️ **A relative or truncated `comm` is an error, never a path.** `ps -o comm=` prints the
    command name alone when the full path does not fit its column width (the `claude-wr` shape
    `lsof` shows), and `go version -m claude-wr` would then read a *different* binary off
    `$PATH` — a reading about some other program, reported as this one.
    """
    if ps is not None:
        out, error = ps, None
    else:
        out, error = _run(["ps", "-o", "comm=", "-p", str(pid)])
    if error:
        return None, f"could not resolve the binary for pid {pid}: {error}"
    path = (out or "").strip()
    if not path:
        return None, f"pid {pid} resolved to no command"
    if not path.startswith("/"):
        return None, (
            f"pid {pid} resolved to a truncated command name ({path!r}), not a path — "
            "re-run with --binary <path>"
        )
    return path, None


def parse_build_info(text):
    """The `go version -m` reading: module, version, revision.

    Lines are tab-separated: `path<TAB>…`, `mod<TAB><module><TAB><version><TAB><hash>`,
    `build<TAB>vcs.revision=<sha>`, `build<TAB>vcs.time=<iso>`. `mod` carries a real version
    when the binary was built from a module version and `(devel)` when it was built from a
    working tree — the two are kept distinct, because only the first can ever equal a tag.
    """
    info = {"go": None, "module": None, "version": None, "revision": None, "vcs_time": None}
    for index, line in enumerate(str(text).splitlines()):
        fields = line.strip().split("\t")
        if index == 0 and ": " in line:
            info["go"] = line.rsplit(": ", 1)[-1].strip()
            continue
        if len(fields) >= 2 and fields[0] == "path":
            info["module"] = fields[1]
        elif len(fields) >= 3 and fields[0] == "mod":
            info["module"] = fields[1]
            info["version"] = fields[2]
        elif len(fields) >= 2 and fields[0] == "build":
            key, _, value = fields[1].partition("=")
            if key == "vcs.revision":
                info["revision"] = value
            elif key == "vcs.time":
                info["vcs_time"] = value
    return info


def read_build_info(binary, dump=None):
    """(info, error) — `go version -m` over the resolved binary, or a recorded dump."""
    if dump is not None:
        return parse_build_info(dump), None
    out, error = _run(["go", "version", "-m", binary])
    if error:
        return None, f"could not read build info from {binary}: {error}"
    info = parse_build_info(out)
    if not info.get("version"):
        return None, f"{binary} carries no module version in its build info"
    return info, None


def parse_releases(text):
    """The released tags, newest first, from `gh release list` (JSON or plain).

    Plain output is `v0.48.0<TAB>Latest<TAB>v0.48.0<TAB>2026-11-20T09:05:57Z`; the JSON form is
    a list of `{"tagName": …}`. Both are accepted so a recorded dump from either form can be
    replayed, which is what makes the comparison testable without a network.
    """
    body = str(text).strip()
    if not body:
        return []
    if body[0] in "[{":
        try:
            data = json.loads(body)
        except ValueError:
            return []
        if isinstance(data, dict):
            data = [data]
        return [str(entry.get("tagName") or entry.get("tag_name") or "")
                for entry in data if isinstance(entry, dict)]
    tags = []
    for line in body.splitlines():
        fields = line.split()
        if fields and fields[0].startswith("v"):
            tags.append(fields[0])
    return tags


def read_releases(repo, dump=None):
    """(tags, error) — the repo's released tags, newest first."""
    if dump is not None:
        tags = parse_releases(dump)
        return (tags, None) if tags else ([], "the recorded release list carried no tags")
    out, error = _run(
        ["gh", "-R", repo, "release", "list", "--limit", str(RELEASE_WINDOW),
         "--json", "tagName"],
        timeout=GH_TIMEOUT,
    )
    if error:
        return [], f"could not read the releases of {repo}: {error}"
    tags = parse_releases(out)
    if not tags:
        return [], f"{repo} returned no releases"
    return tags, None


def _semver(tag):
    match = RELEASE_RE.match(str(tag))
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def classify_version(version):
    """`release` / `pseudo` / `devel` / `unknown`, and the base a gap is counted from."""
    if version is None:
        return "unknown", None
    if RELEASE_RE.match(version):
        return "release", version
    pseudo = PSEUDO_RE.match(version)
    if pseudo:
        return "pseudo", "v" + pseudo.group(1)
    if version in ("(devel)", "devel"):
        return "devel", None
    return "unknown", None


def compare(running, releases):
    """The reading: state, both versions, and the gap.

    ⚠️ **Agreement is exact equality of a RELEASE tag with the latest release**, never a
    prefix or a semver-ordering coincidence. `v0.48.0` against `v0.48.0` agrees; a
    pseudo-version whose base happens to be `v0.48.0` does **not**, because it was built from a
    commit no tag points at — which is the defect this exists to name.
    """
    tags = [t for t in (releases or []) if _semver(t)]
    latest = tags[0] if tags else None
    kind, base = classify_version(running)
    reading = {
        "state": UNREAD,
        "running": running,
        "running_kind": kind,
        "running_base": base,
        "released": latest,
        "gap": None,
        "releases_checked": len(tags),
    }
    if latest is None:
        reading["error"] = "no released tag to compare against"
        return reading
    if running is None:
        reading["error"] = "no running version to compare"
        return reading

    if running == latest and kind == "release":
        reading["state"] = AGREEMENT
        reading["gap"] = 0
        return reading

    reading["state"] = DIVERGENT
    # The gap counts releases strictly newer than the version the running build derives from.
    if base is not None:
        base_key = _semver(base)
        if base_key is not None:
            reading["gap"] = sum(1 for tag in tags if _semver(tag) > base_key)
    return reading


def probe(port=DEFAULT_PORT, repo=DEFAULT_REPO, pid=None, binary=None,
          build_info_dump=None, releases_dump=None, running_override=None,
          released_override=None, lsof=None, ps=None):
    """The whole reading, with every side's failure named rather than folded away.

    ⚠️ **A named `binary` skips the port lookup.** The port is how the binary is *found* when
    nobody names it; once `--binary` names it there is nothing left for `lsof` to answer, and
    requiring the lookup anyway would make a recorded build-info dump untestable without a
    live listener.
    """
    overrides = {}
    result = {
        "port": port,
        "repo": repo,
        "pid": None,
        "pids": [],
        "binary": None,
        "module": None,
        "revision": None,
        "vcs_time": None,
        "overrides": overrides,
    }

    if running_override is not None:
        overrides["running"] = True
        result["pid"] = pid
        result["binary"] = binary
    else:
        if binary is None:
            pids, error = resolve_pid(port, lsof=lsof)
            if error:
                result.update(compare(None, None))
                result["state"] = UNREAD
                result["error"] = error
                return result
            result["pids"] = pids
            result["pid"] = pid or pids[0]
            path, error = resolve_binary(result["pid"], ps=ps)
        else:
            path, error = binary, None
            result["pid"] = pid
        if error:
            result.update(compare(None, None))
            result["state"] = UNREAD
            result["error"] = error
            return result
        result["binary"] = path
        info, error = read_build_info(path, dump=build_info_dump)
        if error:
            result.update(compare(None, None))
            result["state"] = UNREAD
            result["error"] = error
            return result
        result["module"] = info.get("module")
        result["revision"] = info.get("revision")
        result["vcs_time"] = info.get("vcs_time")
        running_override = info.get("version")

    if released_override is not None:
        overrides["released"] = True
        tags = [released_override]
    else:
        tags, error = read_releases(repo, dump=releases_dump)
        if error:
            result.update(compare(running_override, None))
            result["state"] = UNREAD
            result["error"] = error
            return result

    result.update(compare(running_override, tags))
    result["checked_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    return result


def render(result):
    """The human report — a named state first, then both sides and the gap."""
    lines = [f"MCP SERVER VERSION: {result['state']}"]
    if result.get("binary"):
        where = f"pid {result['pid']}, port {result['port']}" if result.get("pid") else \
            f"port {result['port']}"
        lines.append(f"  binary:   {result['binary']} ({where})")
    running = result.get("running") or "unknown"
    if result.get("running_kind") == "pseudo":
        running += " (pseudo-version, commit %s)" % (result.get("revision") or "?")[:12]
    elif result.get("running_kind") == "devel":
        running += " (built from a working tree)"
    elif result.get("running_kind") == "release":
        running += " (released tag)"
    lines.append(f"  running:  {running}")
    lines.append(f"  released: {result.get('released') or 'unknown'}")
    if result["state"] == AGREEMENT:
        lines.append("  gap:      0")
    elif result["state"] == DIVERGENT:
        gap = result.get("gap")
        lines.append(f"  gap:      {gap} release(s) behind" if gap is not None
                     else "  gap:      unknown — the running base is outside the fetched window")
    else:
        lines.append(f"  why:      {result.get('error') or 'the reading could not be taken'}")
    for side in sorted(result.get("overrides") or {}):
        flag = {"running": "--running-version", "released": "--released-tag"}.get(side, side)
        lines.append(f"  ⚠️ {side} version was FED ({flag}), not measured")
    return "\n".join(lines)


def render_line(result):
    """One line for a round's own render — the state, both sides, and the gap."""
    running = result.get("running") or "unknown"
    if result.get("running_kind") == "pseudo":
        running += f" (commit {(result.get('revision') or '?')[:12]})"
    elif result.get("running_kind") == "devel":
        running += " (devel)"
    released = result.get("released") or "unknown"
    if result["state"] == AGREEMENT:
        return f"MCP server {running} · released {released} · agreement, gap 0"
    if result["state"] == DIVERGENT:
        gap = result.get("gap")
        gap_text = f"gap {gap}" if gap is not None else "gap unknown"
        return f"MCP server {running} · released {released} · DIVERGENT, {gap_text}"
    return f"MCP server {running} · released {released} · UNREAD — {result.get('error')}"


def card_payload(result):
    """The card's text: what runs, what was released, and how far apart they are."""
    gap = result.get("gap")
    gap_text = f"{gap} release(s) behind" if gap is not None else "distance unknown"
    return (
        f"MCP server runs {result.get('running')} "
        f"({result.get('running_kind')}-version, commit "
        f"{(result.get('revision') or '?')[:12]}) while {result.get('repo')} has released "
        f"{result.get('released')} — {gap_text}. Binary: {result.get('binary')} "
        f"(pid {result.get('pid')}, port {result.get('port')})."
    )


def post(result, producer_kind="manager"):
    """Post one `ack` card for a divergence. Returns (item_id, error).

    ⚠️ **Deduped on the version pair, not on a constant.** The store suppresses a duplicate
    push on the same `producer_id` + `dedup_key`, so a per-tick caller posts one card per
    divergence; a *new* divergence (either side moved) is a new key and does reach the board.
    """
    if result.get("state") != DIVERGENT:
        return None, "only a divergence is posted"
    # ⚠️ A FED reading never reaches the board. The overrides exist to prove the comparison's
    # other branch; a card built from one would report a divergence nobody measured, and a
    # dedup key on fabricated versions would keep it there.
    if result.get("overrides"):
        return None, "refusing to post a card from a FED reading (--running-version/--released-tag)"
    key = re.sub(r"[^a-z0-9]+", "-",
                 f"mcp-version-gap {result.get('running')} {result.get('released')}".lower()).strip("-")
    cmd = [
        sys.executable, ASK, "post",
        "--dedup-key", key[:120],
        "--payload", card_payload(result),
        "--context", f"mcp-version-gap.py · {result.get('binary')}",
        "--mechanism", "ack",
        "--producer-kind", producer_kind,
        # Self-report, not enumerated by the store; `approve` is the rung the watcher's own
        # ack cards use (`docs/rebase-agent.md` § the board fields).
        "--interrupt-class", "approve",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        return None, f"could not run attention-ask.py: {error}"
    if proc.returncode != 0:
        detail = (proc.stderr.strip().splitlines() or ["(no output)"])[-1]
        return None, f"attention-ask.py exited {proc.returncode}: {detail}"
    for line in (proc.stdout or "").splitlines():
        if line.startswith("ITEM_ID:"):
            return line[len("ITEM_ID:"):].strip(), None
    return None, f"attention-ask.py printed no ITEM_ID: {(proc.stdout or '').strip()[:120]}"


EXIT = {AGREEMENT: 0, DIVERGENT: 1, UNREAD: 2}


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Does the MCP server's binary match a release?")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--pid", type=int, help="skip lsof and use this pid")
    parser.add_argument("--binary", help="skip ps and use this binary path")
    parser.add_argument("--build-info", dest="build_info", metavar="FILE",
                        help="read a recorded `go version -m` dump instead of running go")
    parser.add_argument("--releases-json", dest="releases_json", metavar="FILE",
                        help="read a recorded `gh release list` dump instead of calling gh")
    parser.add_argument("--running-version", dest="running_version",
                        help="FED: compare this version as the running side (agreement proof)")
    parser.add_argument("--released-tag", dest="released_tag",
                        help="FED: compare against this tag as the released side")
    parser.add_argument("--line", action="store_true", help="print ONE line for a round's render")
    parser.add_argument("--json", action="store_true", help="print the reading as JSON")
    parser.add_argument("--post", action="store_true",
                        help="post an ack card on divergence (deduped on the version pair)")
    parser.add_argument("--producer-kind", default="manager",
                        choices=("session", "manager", "agent", "cron", "dark-factory"))
    args = parser.parse_args(argv)

    build_info_dump = _read(args.build_info) if args.build_info else None
    releases_dump = _read(args.releases_json) if args.releases_json else None

    result = probe(
        port=args.port,
        repo=args.repo,
        pid=args.pid,
        binary=args.binary,
        build_info_dump=build_info_dump,
        releases_dump=releases_dump,
        running_override=args.running_version,
        released_override=args.released_tag,
    )

    if args.post:
        item_id, error = post(result, producer_kind=args.producer_kind)
        if item_id:
            result["card"] = item_id
        elif error and result["state"] == DIVERGENT:
            result["card_error"] = error

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.line:
        print(render_line(result))
        if result.get("card"):
            print(f"CARD: {result['card']}")
        elif result.get("card_error"):
            print(f"CARD FAILED: {result['card_error']}", file=sys.stderr)
    else:
        print(render(result))
        if result.get("card"):
            print(f"  card:     {result['card']}")
        elif result.get("card_error"):
            print(f"  card:     FAILED — {result['card_error']}", file=sys.stderr)
    return EXIT.get(result["state"], 2)


if __name__ == "__main__":
    sys.exit(main())
