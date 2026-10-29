#!/usr/bin/env python3
"""Count the prod, irreversible or scope-changing actions a manager approved.

The goal criterion this feeds — *"an audit of the same window finds 0 prod,
irreversible or scope-changing actions approved by a manager"* — had a baseline
measured once, by hand, and nothing on disk emitted it afterwards. A criterion
with no producer is either quietly assumed met or quietly ignored.

The number is worthless unless the classifier is pinned before it runs, so the
expected verdicts live in a committed fixture (`scripts/tests/fixtures/
permission-violations.json`) that was written in its own commit, ahead of this
file. The probe re-runs that fixture on every invocation and **refuses to print
a count** if any case disagrees. Two of those cases are controls:

  known-positive  `rm -rf /tmp/x`  must classify irreversible
  known-negative  `ls /tmp`        must classify clean

A control is made to disagree **only** by mutating its fixture entry's
`expected` value — there is no env override and no code toggle. Three hand
passes produced three different wrong numbers before the controls existed (task
names containing "prod"; a missed `kubectlprod … rollout` and a missed
`cd … && git push`; prod reads counted as prod actions), and none of them were
visible without one.

The classifier reads `tool` and `key` only. `label` is deliberately ignored: a
task name carrying "prod" is not a prod action.

Known limit: a shell payload quoted inside a tool that is not a shell
(`python3 -c '…'`, an awk program) is opaque and is not inspected. `sh`/`bash`
`-c` payloads are inspected one level deep.
"""
import argparse
import json
import re
import shlex
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = REPO / "scripts" / "tests" / "fixtures" / "permission-violations.json"
DEFAULT_LOG = Path.home() / ".local" / "state" / "claude-supervisor" / "permissions.jsonl"

VERDICTS = ("clean", "irreversible", "scope-changing", "prod")
#: Higher wins when one record carries several violations.
PRECEDENCE = {"clean": 0, "prod": 1, "scope-changing": 2, "irreversible": 3}

PROD_WRAPPERS = {"kubectlprod", "kubectlnukeprod", "kubectlsmallprod"}
ALL_WRAPPERS = PROD_WRAPPERS | {
    "kubectl",
    "kubectldev",
    "kubectlnukedev",
    "kubectlquant",
    "kubectlhell",
    "kubectlsun",
    "kubectlstaging",
}
READ_VERBS = {
    "get", "describe", "logs", "top", "explain", "api-resources", "version",
    "config", "auth", "cluster-info", "diff", "wait",
}
MUTATING_VERBS = {
    "apply", "create", "patch", "edit", "scale", "exec", "replace",
    "annotate", "label", "set", "cordon", "uncordon", "expose", "run",
}
IRREVERSIBLE_KUBECTL_VERBS = {"rollout", "delete", "drain"}

#: A key that changes the policy or the plan governing the fleet.
SCOPE_CHANGING_PATHS = re.compile(
    r"(?:server/policy\.json|server/manager-allowlist\.json|"
    r"(?:^|/)\.?claude/settings(?:\.local)?\.json)$"
)

_HEREDOC = re.compile(r"<<-?\s*'?([A-Za-z_][A-Za-z0-9_]*)'?")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}


def strip_heredocs(command):
    """Drop heredoc bodies so prose inside them cannot look like a command."""
    kept = []
    terminator = None
    for line in command.split("\n"):
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        match = _HEREDOC.search(line)
        if match:
            terminator = match.group(1)
        kept.append(line)
    return "\n".join(kept)


def split_segments(command, _depth=0):
    """Split a shell command into argv lists on newlines, `&&`, `||`, `;` and `|`.

    Newlines are a separator in their own right: shlex treats one as plain
    whitespace, so a multi-line script would otherwise collapse into a single
    argv whose leading token is only the first line's command. Measured on the
    2026-10-29 window: a `set -e` script carrying `rm -rf /tmp/pa-origin` on its
    fourth line was invisible that way. Backslash-newline continuations are
    joined first so they do not split a command in half.
    """
    if _depth > 2:
        return []
    joined = re.sub(r"\\\n", " ", strip_heredocs(command))
    segments = []
    for line in joined.split("\n"):
        segments.extend(_split_line(line))
    return segments


def _split_line(line):
    """Split one line into argv lists on `&&`, `||`, `;` and `|`."""
    lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    segments, current = [], []
    try:
        for token in lexer:
            if token and set(token) <= set(";&|"):
                if current:
                    segments.append(current)
                current = []
            else:
                current.append(token)
    except ValueError:
        # An unbalanced quote: fall back to a conservative whitespace split so a
        # malformed command is still inspected rather than silently skipped.
        return [line.split()] if line.split() else []
    if current:
        segments.append(current)
    return segments


def _leading(argv):
    """Return (command, args) after any leading VAR=value assignments."""
    index = 0
    while index < len(argv) and _ENV_ASSIGN.match(argv[index]):
        index += 1
    if index >= len(argv):
        return None, []
    return argv[index], argv[index + 1:]


def _rm_is_forced(args):
    for arg in args:
        if arg == "--recursive" or arg == "--force":
            return True
        if arg.startswith("-") and not arg.startswith("--"):
            letters = arg.lstrip("-")
            if "r" in letters or "R" in letters or "f" in letters:
                return True
    return False


def _classify_kubectl(command, args):
    verb = next((tok for tok in args if tok in READ_VERBS | MUTATING_VERBS | IRREVERSIBLE_KUBECTL_VERBS), None)
    if verb is None:
        return None
    if verb in IRREVERSIBLE_KUBECTL_VERBS:
        return "irreversible"
    if verb == "exec":
        if "--" in args:
            inner = args[args.index("--") + 1:]
            return classify_argv(inner, _depth=1)
        return None
    if verb in MUTATING_VERBS:
        return "prod" if command in PROD_WRAPPERS else None
    return None


def classify_argv(argv, _depth=0):
    """Return the verdict for one shell segment, or None when it is clean."""
    command, args = _leading(argv)
    if command is None:
        return None

    if command == "rm" and _rm_is_forced(args):
        return "irreversible"

    if command == "git":
        words = [tok for tok in args if not tok.startswith("-")]
        if words[:1] == ["push"]:
            return "irreversible"
        if words[:1] == ["mv"] and any(tok in ("-f", "--force") for tok in args):
            return "irreversible"
        if words[:1] == ["reset"] and "--hard" in args:
            return "irreversible"
        if words[:1] == ["clean"] and any(tok.startswith("-") and "f" in tok for tok in args):
            return "irreversible"

    if command in ALL_WRAPPERS:
        verdict = _classify_kubectl(command, args)
        if verdict:
            return verdict

    if command == "vault-cli":
        if args[:1] == ["task"]:
            if "set" in args and ("phase" in args or "status" in args):
                return "scope-changing"
            if args[1:2] == ["complete"]:
                return "scope-changing"

    if command == "make" and any(tok in ("apply", "buca") for tok in args):
        if "BRANCH=prod" in argv or any("BRANCH=prod" in tok for tok in args):
            return "prod"

    if command in _SHELLS and "-c" in args and _depth < 2:
        index = args.index("-c")
        if index + 1 < len(args):
            worst = None
            for segment in split_segments(args[index + 1], _depth=_depth + 1):
                verdict = classify_argv(segment, _depth=_depth + 1)
                if verdict and (worst is None or PRECEDENCE[verdict] > PRECEDENCE[worst]):
                    worst = verdict
            return worst

    return None


def classify_record(record):
    """The verdict for one permissions.jsonl record: a manager-approved action only."""
    if record.get("decided_by") != "manager" or record.get("decision") != "allow":
        return "clean"
    tool = record.get("tool")
    key = record.get("key") or ""
    if tool in ("Edit", "Write") and SCOPE_CHANGING_PATHS.search(key):
        return "scope-changing"
    if tool != "Bash":
        return "clean"
    worst = "clean"
    for segment in split_segments(key):
        verdict = classify_argv(segment)
        if verdict and PRECEDENCE[verdict] > PRECEDENCE[worst]:
            worst = verdict
    return worst


def run_fixture(fixture):
    """Return (results, failures) for every fixture case."""
    results = []
    for case in fixture.get("cases", []):
        actual = classify_record(case["record"])
        results.append(
            {
                "name": case["name"],
                "expected": case["expected"],
                "actual": actual,
                "ok": actual == case["expected"],
                "why": case.get("why", ""),
            }
        )
    return results, [row for row in results if not row["ok"]]


def _parse_ts(value):
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def read_window(log_path, start, end):
    records = []
    with open(log_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            ts = record.get("ts")
            if not ts:
                continue
            try:
                when = _parse_ts(ts)
            except ValueError:
                continue
            if start <= when <= end:
                records.append(record)
    return records


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG, help="permissions.jsonl to read")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE, help="expected-verdict fixture")
    parser.add_argument("--from", dest="start", help="window start, ISO-8601 (default: 7 days before --to)")
    parser.add_argument("--to", dest="end", help="window end, ISO-8601 (default: now)")
    parser.add_argument("--json", action="store_true", help="emit one JSON object instead of text")
    args = parser.parse_args(argv)

    end = _parse_ts(args.end) if args.end else datetime.now(timezone.utc)
    start = _parse_ts(args.start) if args.start else end - timedelta(days=7)

    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    results, failures = run_fixture(fixture)

    if failures:
        for row in results:
            mark = "ok  " if row["ok"] else "FAIL"
            print(f"  [{mark}] {row['name']}: expected {row['expected']}, got {row['actual']}", file=sys.stderr)
        print(
            "CONTROL FAILED — a fixture case disagrees with its committed expected verdict. "
            "Refusing to print a count.",
            file=sys.stderr,
        )
        return 2

    records = read_window(args.log, start, end)
    manager_records = [r for r in records if r.get("decided_by") == "manager"]
    violations = [r for r in records if classify_record(r) != "clean"]

    counts = {verdict: 0 for verdict in VERDICTS if verdict != "clean"}
    for record in violations:
        counts[classify_record(record)] += 1

    controls = fixture.get("controls", {})
    payload = {
        "window_start": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "window_end": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "records": len(records),
        "manager_records": len(manager_records),
        "violations": len(violations),
        "counts": counts,
        "controls": {
            "cases": len(results),
            "passed": len(results) - len(failures),
            "known_positive": controls.get("known_positive"),
            "known_negative": controls.get("known_negative"),
        },
    }

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"window:        {payload['window_start']} .. {payload['window_end']}")
    print(f"records:       {payload['records']} (manager: {payload['manager_records']})")
    print(f"controls:      {payload['controls']['passed']}/{payload['controls']['cases']} fixture cases pass")
    print(f"violations:    {payload['violations']}")
    for verdict in ("irreversible", "scope-changing", "prod"):
        print(f"  {verdict}: {counts[verdict]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
