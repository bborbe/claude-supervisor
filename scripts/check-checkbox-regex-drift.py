#!/usr/bin/env python3
"""`_VERB_BOX` must stay byte-identical to vault-cli's `storage.CheckboxRegex`.

`scripts/goal-closure-read.py` mirrors `vault-cli task complete`'s gate with a
**hand-copied** regex. The copy predicts the verb only for as long as the two literals
agree, and nothing else in this repo can see the other side: vault-cli is a Go module,
the reader is a Python script, and no import crosses that boundary. Without this check an
upstream edit lands silently and the reader starts reporting CLOSE for pages the verb
refuses — the failure that costs a repeated closure attempt on every tick.

**Fails closed.** An unreachable upstream is not evidence of agreement. If the fetch
errors, times out, or returns a page carrying no `CheckboxRegex`, this check fails and
names the reason. The alternative — skipping when the network is down — makes the guard
pass exactly when it can prove nothing, which reads identically to a real green.

**What this proves, and what it does not.** It proves the two literals agree at the
moment it runs. It does not prove either is correct, and it cannot catch a shape both
sides get wrong. The other half of the guard — pinning the divergence between the reader's
own `_BOX` and `_VERB_BOX` — lives in
`scripts/tests/test_goal_closure_read.py::DivergenceClassTest`.
"""

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
READER = REPO / "scripts" / "goal-closure-read.py"
# Deliberately `master`, not a tag. A pinned tag would compare against a released vault-cli
# the installed binary may not be — a guaranteed stale comparison, which is the drift this
# check exists to catch. `master` is the right source for "does the copy still match
# upstream", and a branch rename fails closed with the fetch error named rather than
# passing. What it cannot prove is that the *installed* vault-cli carries the same regex;
# that is a different question, and a tag pin would not answer it either.
UPSTREAM = "https://raw.githubusercontent.com/bborbe/vault-cli/master/pkg/storage/base.go"
TIMEOUT_SECONDS = 10

# `_VERB_BOX = re.compile(r"…", re.M)` — the raw-string body, backslashes intact.
# Anchored under `re.M` so a differently-named assignment cannot satisfy it: unanchored,
# `MY_VERB_BOX = …` matches on the `_VERB_BOX` substring and the guard reports `ok` for a
# line that is not the mirror at all.
LOCAL = re.compile(r'^\s*_VERB_BOX\s*=\s*re\.compile\(r"([^"]*)"', re.M)
# `CheckboxRegex = regexp.MustCompile(`…`)` — the backtick raw-string body, tab-indented
# in the Go source, so `^\s*` rather than `^`.
REMOTE = re.compile(r"^\s*CheckboxRegex\s*=\s*regexp\.MustCompile\(`([^`]*)`\)", re.M)


def fetch(url: str) -> str:
    """`curl`, not `urllib`: the macOS framework Python carries no CA bundle, so `urllib`
    fails CERTIFICATE_VERIFY_FAILED on a machine where `curl` succeeds. Measured on this
    repo's own first run — the check is worthless if it cannot reach its source.

    `--proto`/`--proto-redir` pin both the request and any redirect to HTTPS: `-L` alone
    would follow an HTTPS→HTTP downgrade, and the check trusts the fetched bytes enough to
    fail the build on them. `-q` must be first — without it curl reads `~/.curlrc`, which
    can inject `--insecure` / `--proxy` / `--cacert` and undo that pinning."""
    result = subprocess.run(
        [
            "curl",
            "-q",
            "-fsSL",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--max-time",
            str(TIMEOUT_SECONDS),
            url,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or f"curl exited {result.returncode}")
    return result.stdout


def main() -> int:
    try:
        local_text = READER.read_text(encoding="utf-8")
    except OSError as err:
        print(
            f"checkbox-regex-drift FAILED: cannot read {READER.name} — {err}",
            file=sys.stderr,
        )
        return 1
    local = LOCAL.search(local_text)
    if local is None:
        print(
            "checkbox-regex-drift FAILED: no `_VERB_BOX = re.compile(r\"...\")` in "
            f"{READER.name} — the mirror moved or was renamed",
            file=sys.stderr,
        )
        return 1

    try:
        remote_text = fetch(UPSTREAM)
    except OSError as err:
        print(
            "checkbox-regex-drift FAILED: cannot read the upstream regex, so drift cannot be "
            f"ruled out — {err}",
            file=sys.stderr,
        )
        print(f"  upstream: {UPSTREAM}", file=sys.stderr)
        return 1

    remote = REMOTE.search(remote_text)
    if remote is None:
        print(
            "checkbox-regex-drift FAILED: upstream carries no "
            "`CheckboxRegex = regexp.MustCompile(`...`)` — the literal moved or was renamed",
            file=sys.stderr,
        )
        print(f"  upstream: {UPSTREAM}", file=sys.stderr)
        return 1

    if local.group(1) != remote.group(1):
        print("checkbox-regex-drift FAILED: the copy has drifted from its source", file=sys.stderr)
        print(f"  _VERB_BOX (scripts/goal-closure-read.py): {local.group(1)}", file=sys.stderr)
        print(f"  storage.CheckboxRegex (vault-cli):        {remote.group(1)}", file=sys.stderr)
        print(
            "  Update _VERB_BOX to match, or update vault-cli if the change is intended.",
            file=sys.stderr,
        )
        return 1

    print("  checkbox-regex-drift ok: _VERB_BOX matches vault-cli's storage.CheckboxRegex")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
