#!/usr/bin/env python3
"""Count the prod, irreversible or scope-changing actions a manager approved.

The goal criterion this feeds — *"an audit of the same window finds 0 prod,
irreversible or scope-changing actions approved by a manager"* — had a baseline
measured once, by hand, and nothing on disk emitted it afterwards. A criterion
with no producer is either quietly assumed met or quietly ignored.

The number is worthless unless the classifier is pinned before it runs, so the
expected verdicts live in a committed fixture (`scripts/tests/fixtures/
permission-violations.json`). Its first six cases were written in their own
commit, ahead of this file; the rest were added from the three verdict
definitions after the classifier was shown to miss those shapes, and eight of
the nine failed against it as it then stood. The probe re-runs the whole fixture
on every invocation and **refuses to print a count** if any case disagrees, or if
either named control is absent. Two of the cases are the controls:

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

Known limits, named because a false negative here reads as a clean audit:

  ⚠️ A payload quoted inside a non-shell tool (`python3 -c '…'`, an awk
     program) is opaque and is not inspected. `sh`/`bash` `-c` payloads are.
  ⚠️ Command substitution is inspected one level (`$(…)`, backticks); a nested
     substitution inside a substitution is not.
  ⚠️ A heredoc opener is recognised only outside quotes, and an unterminated one
     drops the rest of the command — conservative, but it can hide a later line.
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
#: `rollout` sub-verbs that only read; the rest replace running pods.
ROLLOUT_READ_SUBVERBS = {"status", "history"}

#: Flags whose value is the next token, for the `kubectl` and `git` positional scans.
KUBECTL_VALUE_FLAGS = {
    "-n", "--namespace", "-l", "--selector", "-o", "--output", "-f", "--filename",
    "--context", "--cluster", "--user", "--kubeconfig", "-c", "--container",
    "--field-selector", "--since", "--type", "-p", "--patch", "--replicas",
    "--image", "--timeout", "--tail", "--request-timeout", "--as", "--as-group",
    "--server", "--token", "--chunk-size",
}
GIT_VALUE_FLAGS = {
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
    "--super-prefix", "--config-env",
}

#: Privilege and scheduling prefixes that do not change the action.
PREFIX_COMMANDS = {"sudo", "env", "timeout", "nice", "nohup", "command", "stdbuf"}
PREFIX_VALUE_FLAGS = {
    "sudo": {"-u", "-g", "-p", "-C", "-D", "-h", "-r", "-t", "-U", "-T", "-R"},
    "nice": {"-n", "--adjustment"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "stdbuf": {"-i", "-o", "-e"},
}
PREFIX_OPERANDS = {"timeout": 1}

#: Tools whose `key` is a path a manager can change policy through.
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

#: A key that changes the policy or the plan governing the fleet. The last two
#: alternatives are the LIVE policy files: `~/.config/claude-supervisor/policy.json`
#: is `config.userPolicy`, checked before the bundled `server/policy.json`.
SCOPE_CHANGING_PATHS = re.compile(
    r"(?:server/policy\.json"
    r"|server/manager-allowlist\.json"
    r"|claude-supervisor/policy\.json"
    r"|(?:^|/)\.claude/settings(?:\.local)?\.json)$"
)

_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_HEREDOC = re.compile(r"<<-?\s*'?([A-Za-z_][A-Za-z0-9_]*)'?")
_SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}


def _heredoc_terminator(line):
    """Return the heredoc terminator if this line opens one outside quotes."""
    in_single = in_double = False
    index = 0
    while index < len(line):
        char = line[index]
        if in_single:
            if char == "'":
                in_single = False
            index += 1
            continue
        if in_double:
            if char == "\\":
                index += 2
                continue
            if char == '"':
                in_double = False
            index += 1
            continue
        if char == "\\":
            index += 2
            continue
        if char == "'":
            in_single = True
        elif char == '"':
            in_double = True
        elif line.startswith("<<", index):
            match = _HEREDOC.match(line[index:])
            return match.group(1) if match else None
        index += 1
    return None


def strip_heredocs(command):
    """Drop heredoc bodies so prose inside them cannot look like a command."""
    kept = []
    terminator = None
    for line in command.split("\n"):
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        terminator = _heredoc_terminator(line)
        kept.append(line)
    return "\n".join(kept)


def substitution_bodies(command):
    """Return the one-level `$(…)` and backtick bodies of a command."""
    return [
        match.group(1) or match.group(2) or ""
        for match in _SUBSTITUTION.finditer(strip_heredocs(command))
    ]


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


def _strip_prefix(argv):
    """Drop `sudo`/`env`/`timeout`/`nice`-style prefixes; they do not change the action."""
    for _ in range(8):
        index = 0
        while index < len(argv) and _ENV_ASSIGN.match(argv[index]):
            index += 1
        if index >= len(argv) or argv[index] not in PREFIX_COMMANDS:
            return argv
        command = argv[index]
        index += 1
        while index < len(argv):
            token = argv[index]
            if command == "env" and _ENV_ASSIGN.match(token):
                index += 1
                continue
            if token.startswith("-"):
                index += 2 if token in PREFIX_VALUE_FLAGS.get(command, set()) else 1
                continue
            break
        index += PREFIX_OPERANDS.get(command, 0)
        argv = argv[index:]
    return argv


def _rm_is_forced(args):
    for arg in args:
        if arg == "--recursive" or arg == "--force":
            return True
        if arg.startswith("-") and not arg.startswith("--"):
            letters = arg.lstrip("-")
            if "r" in letters or "R" in letters or "f" in letters:
                return True
    return False


def _git_subcommand(args):
    """Return the git subcommand, skipping flags and their separate values."""
    index = 0
    while index < len(args):
        token = args[index]
        if token in GIT_VALUE_FLAGS:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token, args[index + 1:]
    return None, []


def _kubectl_verb(args):
    """Return (verb, index) at its POSITION, skipping flags and their values."""
    index = 0
    while index < len(args):
        token = args[index]
        if token in KUBECTL_VALUE_FLAGS:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token, index
    return None, None


def _exec_inner(args):
    """The command after `kubectl exec POD [flags] [--] CMD…`, with or without `--`."""
    index = 0
    pod_seen = False
    while index < len(args):
        token = args[index]
        if token == "--":
            index += 1
            break
        if token in KUBECTL_VALUE_FLAGS:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        if not pod_seen:
            pod_seen = True
            index += 1
            continue
        break
    return args[index:]


def _classify_kubectl(command, args):
    verb, verb_index = _kubectl_verb(args)
    if verb is None:
        return None
    if verb in IRREVERSIBLE_KUBECTL_VERBS:
        if verb == "rollout":
            rest = [tok for tok in args[verb_index + 1:] if not tok.startswith("-")]
            if rest and rest[0] in ROLLOUT_READ_SUBVERBS:
                return None
        return "irreversible"
    if verb == "exec":
        inner = _exec_inner(args[verb_index + 1:])
        verdict = classify_argv(inner, _depth=1) if inner else None
        if verdict:
            return verdict
        return "prod" if command in PROD_WRAPPERS else None
    if verb in MUTATING_VERBS:
        return "prod" if command in PROD_WRAPPERS else None
    return None


def classify_argv(argv, _depth=0):
    """Return the verdict for one shell segment, or None when it is clean."""
    if _depth > 3 or not argv:
        return None
    argv = _strip_prefix(argv)
    command, args = _leading(argv)
    if command is None:
        return None

    if command == "rm" and _rm_is_forced(args):
        return "irreversible"

    if command == "git":
        subcommand, rest = _git_subcommand(args)
        if subcommand == "push":
            return "irreversible"
        if subcommand == "mv" and any(tok in ("-f", "--force") for tok in args):
            return "irreversible"
        if subcommand == "reset" and "--hard" in args:
            return "irreversible"
        if subcommand == "clean" and any(tok.startswith("-") and "f" in tok for tok in args):
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
        if any("BRANCH=prod" in tok for tok in argv):
            return "prod"

    if command in _SHELLS and "-c" in args and _depth < 3:
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
    if tool in WRITE_TOOLS and SCOPE_CHANGING_PATHS.search(key):
        return "scope-changing"
    if tool != "Bash":
        return "clean"
    segments = split_segments(key)
    for body in substitution_bodies(key):
        segments.extend(split_segments(body))
    worst = "clean"
    for segment in segments:
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


def missing_controls(fixture):
    """Names of the required controls the fixture does not define as cases."""
    names = {case["name"] for case in fixture.get("cases", [])}
    controls = fixture.get("controls", {})
    missing = []
    for role in ("known_positive", "known_negative"):
        name = controls.get(role)
        if not name or name not in names:
            missing.append(f"{role} ({name or 'unset'})")
    return missing


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

    try:
        fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"REFUSED — cannot read the fixture {args.fixture}: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"REFUSED — the fixture {args.fixture} is not valid JSON: {exc}", file=sys.stderr)
        return 2

    missing = missing_controls(fixture)
    if missing:
        print(
            "CONTROL FAILED — the fixture does not define its required controls: "
            + ", ".join(missing)
            + ". Refusing to print a count.",
            file=sys.stderr,
        )
        return 2

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

    try:
        records = read_window(args.log, start, end)
    except OSError as exc:
        print(f"REFUSED — cannot read {args.log}: {exc}", file=sys.stderr)
        return 2

    manager_records = [r for r in records if r.get("decided_by") == "manager"]
    violations = []
    for record in records:
        verdict = classify_record(record)
        if verdict != "clean":
            violations.append((record, verdict))

    counts = {verdict: 0 for verdict in VERDICTS if verdict != "clean"}
    for _, verdict in violations:
        counts[verdict] += 1

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
