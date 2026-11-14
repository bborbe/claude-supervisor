#!/usr/bin/env python3
"""Rebase every behind-master open PR in a scratch worktree, gate it, hand the operator a line.

Rationale, repo set, conflict table and the live-queue exercise: `docs/rebase-agent.md`.

Why this exists
---------------
A release cut renames the top `## Unreleased` to `## vX.Y.Z`, and every open branch
still carries `## Unreleased`. Each merge therefore re-conflicts every other open PR.
Measured 2026-11-06 in `bborbe/claude-supervisor`: **8 of 12 open PRs DIRTY, 4 of them
APPROVED**, and four merges that evening re-broke the set four times.

⚠️ **The trap is the mechanism, not the conflict.** "Sync the branch with master"
naturally means `git merge origin/master`, and that writes a merge commit. Measured on
`#652`: three merge commits (`4ead1e56`, `da40e840`, `c1e51e25`), head APPROVED, PR
`MERGEABLE`/`CLEAN` — it would merge cleanly and pass every visible check while
permanently failing the merge train's linearity probe. This script therefore never
merges; it rebases, and the linearity gate below is what proves it.

What it does, and what it refuses to do
---------------------------------------
Per candidate it creates a **detached** worktree at a temp path — no branch ref exists
to damage, and the main worktree is never entered — rebases onto `origin/master`, runs
the gates, and emits one push line or a failure report. It **never pushes** and **never
merges**; a failed gate blocks, it does not warn.

Conflict policy, and the line between mechanical and genuine
-----------------------------------------------------------
A conflict outside `CHANGELOG.md` is genuine: refuse, name the paths, let a worker
resolve. A `CHANGELOG.md`-only conflict is the mechanical class this agent exists to
clear, resolved deterministically from the branch's own added bullets — never by
guessing at the author's intent — and refused when the branch does more than add
bullets, because taking master's side would silently discard that edit.

The seven gates
---------------
Each is spelled out, with its trap, in `docs/rebase-agent.md` § The gates. In short:
(1) branch linear, (2) the fold guard prints only `+## Unreleased` and an **empty**
result is never a pass, (3) no master bullet dropped — pattern `^--[^-]`, since a
removed bullet renders as `-- <bullet>`, (4) the `awk` placement assertion is the
**authority** and disambiguates (2), (5) `grep -c '^## Unreleased$'` prints 1 — a
duplicate heading reads as one to (2) and (4), (6) the candidate is under the repo's
park size limit, (7) the rebase leaves a commit ahead of master rather than a no-op.

The park thresholds are the reviewer's deployment env, so the defaults are unverified.
The merge-time twin of (2)–(4) is the coding plugin's `check-changelog-unreleased-fold.sh`;
it is deliberately not called — it predicts what a *merge* would fold, and no merge
happens here. The `awk` form is shared, including its `ENVIRON` trick: `awk -v` turns a
literal backslash-n into a newline, which matches no line and reports a false MISSING.

Out of scope by contract
------------------------
Merge ordering — the agent rebases, never decides what merges when. Prod-pin PRs are the
operator's approval, refused before any rebase.

Exit codes
----------
0  every candidate produced a push line, or there was no candidate
1  at least one candidate refused or failed a gate
2  the queue could not be read — `unknown`, never rendered as "nothing to do"

The run record
--------------
Every run writes `<stamp>.json` and `latest.json` under `--state-dir` and prints one
`record: {json}` line to stdout. The line exists because the state directory does not
survive a container: in the cluster it is an emptyDir, so the pod's log is the only
record that outlives the run. `--trigger` (default `manual`) is written into both, so a
hand-started run cannot be read as a scheduled one.

The board
---------
A push line is the operator's to run, so a record nobody reads is not delivery. Each
`PUSH` row is also posted to the fleet attention board as one card carrying the line
verbatim — when `ATTENTION_STORE_URL` names a store, and `ATTENTION_STORE_TOKEN` a bearer
token for any non-loopback one (an unauthenticated remote store is readable by anything
that can reach it). Unset means no board write plus one line on stderr: this script also
runs on the Mac, and defaulting to `localhost:18080` the way the session producers do
would post every sweep to whatever happens to listen there.

⚠️ A board failure is never a rebase failure. It is logged, written onto the row as
`board_error`, and leaves the verdict alone — the record still carries the line.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

STATE_DIR = os.path.expanduser("~/.local/state/claude-supervisor/rebase-agent")
STATE_KEEP = 50
# The one-line record printed to stdout. A fixed prefix keeps it greppable out of a Job
# log that also carries the human render.
RECORD_PREFIX = "record: "
TRIGGERS = ("manual", "schedule")
# A hung `git rebase` or a stalled `gh` would otherwise hold the launchd job open
# forever; the interval is 900 s, so nothing legitimate takes this long.
TIMEOUT_SECONDS = 300
SCRATCH_REF = "refs/rebase-agent"

# The fleet attention board, reached over its bearer-gated listener (`/api/1.0/attention`).
# The port is 18081 in the cluster, NOT 18080: 18080 is the unauthenticated board page and
# is deliberately not the cluster path.
BOARD_STORE_ENV = "ATTENTION_STORE_URL"
BOARD_TOKEN_ENV = "ATTENTION_STORE_TOKEN"
BOARD_PATH = "/api/1.0/attention"
# A board that is down must cost a clear failure, never a stalled sweep. 10 s matches the
# sibling attention arms, measured against the store's create path peaking at 2.26 s under
# 20 concurrent readers — a timeout here loses the card while the rebase itself succeeded.
BOARD_TIMEOUT_SECONDS = 10
# Every sweep re-pushes while the PR is still behind, and duplicate suppression refreshes
# the card's payload and created_at rather than adding a row, so this only has to outlive
# the 900 s gap between two runs. Once the PR is rebased or merged the re-push stops and
# the card clears itself.
BOARD_CARD_TTL_SECONDS = 3600
BOARD_PRODUCER_ID = "rebase-agent"
BOARD_PRODUCER_KIND = "cron"
# A card is "live" only when the store can resolve its liveness model. It resolves
# `heartbeat:<path>` against the STORE host's filesystem and `session:`/`owner:` against
# the store host's session registry — a pod appears in neither. So `owner:` is the model a
# pod must declare: it probes nothing and returns live unconditionally, which is what lets
# duplicate suppression collapse a re-push onto the existing card instead of adding a new
# one every 15 minutes. The price is that it never expires by liveness, so the card MUST
# carry `expires_at` — that deadline is its only removal.
BOARD_LIVENESS_REF = "owner:operator"
# A push line is a condition the operator acts on; nothing routes back to the pod, so the
# item is an `ack`. `interrupt_class` is a self-report and is not enumerated by the store;
# `approve` is the rung the watcher's own ack cards use.
BOARD_ANSWER_MECHANISM = "ack"
BOARD_INTERRUPT_CLASS = "approve"
BOARD_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

# The transfer channel. The pod writes a git bundle to a PVC, a long-lived reader pod
# mounts that PVC, and the operator's one line copies the bundle off the reader and
# fetches it before pushing. All three values are deployment facts, so they come from the
# environment: a pod cannot infer which `kubectl` wrapper the operator's Mac has, and a
# wrong guess produces a line that fails only after it has been pasted — which reads as a
# broken rebase rather than a missing setting.
BUNDLE_DIR_ENV = "REBASE_BUNDLE_DIR"
KUBECTL_ENV = "REBASE_KUBECTL"
READER_POD_ENV = "REBASE_READER_POD"
# Where the OPERATOR's checkouts live. A deployment fact like the three above, and for the
# same reason: the pod cannot derive it. `expanduser` is wrong here in both directions —
# in the pod it resolves to /workspace and re-creates the defect this channel exists to
# remove, and hard-coding one machine's home directory resolves on exactly one machine.
OPERATOR_WORKSPACES_ENV = "REBASE_OPERATOR_WORKSPACES"
# The bundle is written here on the operator's Mac, one name per PR so a re-sweep
# overwrites the previous copy rather than accumulating files.
LOCAL_BUNDLE = "/tmp/rebase-agent-pr-{number}.bundle"


# The repo set is explicit, never "every repo". The conflict class exists only where a
# release cut renames `## Unreleased` in a CHANGELOG that open branches also edit — a
# repo without that convention cannot produce the conflict, so sweeping it would rebase
# branches for nothing. `vault-cli` is here because it holds the exact pathology: two
# open PRs, both CONFLICTING, both APPROVED.
DEFAULT_REPOS = (
    {
        "repo": "bborbe/claude-supervisor",
        # The pod's clone. `expanduser` is deliberate: HOME is /workspace in the pod, and
        # pointing HOME at the volume is what makes the container layout match a laptop.
        "path": os.path.expanduser("~/Documents/workspaces/claude-supervisor"),
        # The auto-reviewer's park thresholds (MAX_ADDITIONS / MAX_CHANGED_FILES in
        # github-pr-watcher) are deployment env, not repo config, so they cannot be
        # read from here. These are the observed defaults and are marked unverified.
        "max_additions": 2000,
        "max_changed_files": 40,
    },
    {
        "repo": "bborbe/vault-cli",
        "path": os.path.expanduser("~/Documents/workspaces/vault-cli"),
        "max_additions": 2000,
        "max_changed_files": 40,
    },
)

# A prod pin is the operator's approval, never a rebase candidate.
PROD_PIN_PATTERNS = (
    re.compile(r"(^|/)values-prod\.ya?ml$"),
    re.compile(r"(^|/)prod[-_/]"),
    re.compile(r"[-_]prod\.(ya?ml|json|env|tf|sh)$"),
    re.compile(r"(^|/)Makefile\.prod$"),
)
PROD_PIN_HINTS = re.compile(r"prod[-_ ]?pin|pin[-_ ]?prod", re.IGNORECASE)


class Refusal(Exception):
    """A candidate that must not produce a push line."""


def run(cmd, cwd=None, env=None, check=False, timeout=TIMEOUT_SECONDS):
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{' '.join(cmd)} timed out after {timeout}s")
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"{' '.join(cmd)} failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return proc


def git(repo_path, *args, env=None, check=False):
    return run(["git", "-C", repo_path, *args], env=env, check=check)


# --------------------------------------------------------------------------- detection


def list_open_prs(cfg):
    """Every open PR, with the two GitHub fields that say it needs attention.

    `mergeStateStatus == "BEHIND"` is a clean-but-stale branch; `mergeable ==
    "CONFLICTING"` / `mergeStateStatus == "DIRTY"` is a conflicting one. Both are
    checked because GitHub populates them at different moments, and a conflicted
    branch is always behind as well — reporting only BEHIND would miss the queue
    that actually exists.
    """
    proc = run(
        [
            "gh", "pr", "list", "--repo", cfg["repo"], "--state", "open",
            "--limit", "100", "--json",
            "number,title,headRefName,baseRefName,mergeable,mergeStateStatus,"
            "isDraft,reviewDecision,additions,deletions,changedFiles,headRepository",
        ]
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh pr list failed for {cfg['repo']}: {proc.stderr.strip()}")
    return json.loads(proc.stdout or "[]")


def needs_rebase(pr):
    return (
        pr.get("mergeStateStatus") in ("BEHIND", "DIRTY")
        or pr.get("mergeable") == "CONFLICTING"
    )


def prod_pin_reason(pr, changed):
    if PROD_PIN_HINTS.search(pr.get("title") or ""):
        return f"title names a prod pin: {pr['title']}"
    if PROD_PIN_HINTS.search(pr.get("headRefName") or ""):
        return f"branch names a prod pin: {pr['headRefName']}"
    for path in changed:
        for pattern in PROD_PIN_PATTERNS:
            if pattern.search(path):
                return f"touches a prod pin: {path}"
    return None


def changed_paths(cfg, number):
    proc = run(
        ["gh", "pr", "view", str(number), "--repo", cfg["repo"],
         "--json", "files", "--jq", ".files[].path"]
    )
    if proc.returncode != 0:
        # None, not [] — an empty list would silently skip the prod-pin path check and
        # leave the refusal resting on title/branch hints alone.
        return None
    return [line for line in proc.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------------- rebase


def fetch_master(repo_path):
    """Refresh `origin/master` before the sweep.

    The rebase and the behind-count both read the local remote-tracking ref, so a
    stale checkout would rebase onto stale master, pass every gate, and still hand
    out a push line for a branch that is behind the real one. Nothing else refreshes
    this clone between runs, so the fetch is the agent's own responsibility.
    """
    proc = git(repo_path, "fetch", "--no-tags", "origin",
               "+refs/heads/master:refs/remotes/origin/master")
    if proc.returncode != 0:
        raise RuntimeError(f"could not fetch origin/master: {proc.stderr.strip()}")


def fetch_head(repo_path, number):
    """Fetch the PR head by its `refs/pull/N/head`, which works for fork PRs too."""
    ref = f"{SCRATCH_REF}/src/{number}"
    proc = git(repo_path, "fetch", "--no-tags", "origin",
               f"+refs/pull/{number}/head:{ref}")
    if proc.returncode != 0:
        raise Refusal(f"could not fetch refs/pull/{number}/head: {proc.stderr.strip()}")
    return git(repo_path, "rev-parse", ref, check=True).stdout.strip()


def changelog_delta(repo_path, base_sha, head_sha):
    """(added bullets, non-bullet CHANGELOG touched) for this branch.

    Read from the branch's own commits, never from a working tree: the resolution
    must reconstruct exactly what the author wrote, not what a merge left lying
    around. A branch that edits a released section or a non-bullet line is refused
    rather than reconstructed — silently taking master's side would drop that edit.
    """
    proc = git(repo_path, "diff", f"{base_sha}...{head_sha}", "--", "CHANGELOG.md")
    if proc.returncode != 0:
        # Never fall through with no bullets: the resolution would then write master's
        # side and re-apply nothing, and the placement gate would pass vacuously
        # because it has no bullet to look for. Refuse instead of dropping them.
        raise Refusal(
            f"could not read the branch's CHANGELOG delta: {proc.stderr.strip()}"
        )
    added, other = [], False
    for line in proc.stdout.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+- "):
            added.append(line[1:].rstrip())
        elif line.startswith("+"):
            # A heading is the release cut's own business, not an edit to reconstruct:
            # a branch cut before a cut carries `+## Unreleased` and master now holds
            # `## vX.Y.Z`, which is exactly the shape this agent exists to repair.
            # Anything else the branch adds is content the reconstruction cannot
            # reproduce, so it is refused rather than dropped.
            if not line[1:].strip() or line[1:].lstrip().startswith("## "):
                continue
            other = True
        elif line.startswith("-"):
            other = True
    return added, other


def unreleased_heading_index(lines):
    for index, line in enumerate(lines):
        if line.startswith("## "):
            return index if line.strip() == "## Unreleased" else None
    return None


def insert_bullets(text, bullets):
    """Ensure one `## Unreleased` section and put every missing bullet inside it.

    ⚠️ When master already carries `## Unreleased`, the bullets are appended to that
    section — never under a second heading inserted above it. Two sections read as one
    to every heading-level probe, so the duplicate is invisible to probes 2 and 4 and
    has to be guarded separately (`gate_single_unreleased`).
    """
    lines = text.splitlines()
    heading = unreleased_heading_index(lines)
    if heading is None:
        first = next((i for i, line in enumerate(lines) if line.startswith("## ")), None)
        if first is None:
            raise Refusal("CHANGELOG.md carries no `## ` section heading to place bullets under")
        lines.insert(first, "## Unreleased")
        lines.insert(first + 1, "")
        heading = first

    end = len(lines)
    for index in range(heading + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    existing = {line.rstrip() for line in lines[heading + 1:end] if line.startswith("- ")}
    missing = [bullet for bullet in bullets if bullet not in existing]
    if not missing:
        return "\n".join(lines) + ("\n" if text.endswith("\n") else "")

    insert_at = heading + 1
    while insert_at < end and not lines[insert_at].startswith("- "):
        insert_at += 1
    while insert_at < end and lines[insert_at].startswith("- "):
        insert_at += 1
    lines[insert_at:insert_at] = missing
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def resolve_changelog(scratch, bullets):
    """Take master's side of a CHANGELOG conflict, then re-apply the branch's bullets."""
    upstream = git(scratch, "show", ":2:CHANGELOG.md")
    if upstream.returncode != 0:
        raise Refusal("CHANGELOG.md conflict has no upstream side to resolve onto")
    resolved = insert_bullets(upstream.stdout, bullets)
    with open(os.path.join(scratch, "CHANGELOG.md"), "w", encoding="utf-8") as handle:
        handle.write(resolved)
    git(scratch, "add", "CHANGELOG.md", check=True)


def rebase_onto_master(scratch, repo_path, head_sha):
    base_sha = git(repo_path, "rev-parse", "origin/master", check=True).stdout.strip()
    bullets, nonbullet = changelog_delta(repo_path, base_sha, head_sha)

    env = dict(os.environ, GIT_EDITOR="true", GIT_SEQUENCE_EDITOR="true")
    proc = git(scratch, "rebase", "origin/master", env=env)
    stops = 0
    while proc.returncode != 0:
        stops += 1
        if stops > 100:
            git(scratch, "rebase", "--abort")
            raise Refusal("rebase did not converge after 100 stops")
        conflicted = [
            line for line in
            git(scratch, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()
            if line.strip()
        ]
        if not conflicted:
            git(scratch, "rebase", "--abort")
            raise Refusal(f"rebase failed without a conflicted path: {proc.stderr.strip()}")
        genuine = [path for path in conflicted if path != "CHANGELOG.md"]
        if genuine:
            git(scratch, "rebase", "--abort")
            raise Refusal("content conflict in " + ", ".join(genuine))
        if nonbullet:
            git(scratch, "rebase", "--abort")
            raise Refusal(
                "CHANGELOG.md conflict, and the branch does more than add bullets "
                "(an edit outside `- ` lines would be discarded by reconstruction)"
            )
        try:
            resolve_changelog(scratch, bullets)
        except Refusal:
            git(scratch, "rebase", "--abort")
            raise
        proc = git(scratch, "rebase", "--continue", env=env)
    return base_sha


# ------------------------------------------------------------------------------ gates


def gate_linear(scratch):
    out = git(scratch, "log", "--merges", "--oneline", "origin/master..HEAD").stdout.strip()
    if out:
        return "merge commits present: " + "; ".join(out.splitlines()[:5])
    return None


def gate_heading_guard(scratch):
    """Probe 2. Empty is NOT a pass — it defers to the placement assertion."""
    diff = git(scratch, "diff", "origin/master", "--", "CHANGELOG.md").stdout
    headings = [line for line in diff.splitlines() if re.match(r"^[-+]## ", line)]
    unexpected = [line for line in headings if line.strip() != "+## Unreleased"]
    if unexpected:
        return "heading guard: " + " | ".join(unexpected[:5])
    return None


def gate_dropped_bullet(scratch):
    """Probe 3. `^--[^-]` — a removed bullet renders as `-- <bullet>`."""
    diff = git(scratch, "diff", "origin/master", "--", "CHANGELOG.md").stdout
    dropped = [line for line in diff.splitlines() if re.match(r"^--[^-]", line)]
    if dropped:
        return f"dropped {len(dropped)} bullet(s): " + " | ".join(dropped[:3])
    return None


def gate_placement(scratch, bullets):
    """Probe 4 — the authority. Every bullet the branch adds must sit under Unreleased."""
    if not bullets:
        return None
    env = dict(os.environ)
    bad = []
    for bullet in bullets:
        env["WANT"] = bullet
        proc = run(
            ["awk", 'BEGIN{want=ENVIRON["WANT"]} /^## /{sec=$0} $0==want '
                    '{print "sits under: " sec}', "CHANGELOG.md"],
            cwd=scratch, env=env,
        )
        if proc.returncode != 0:
            return f"awk placement assertion failed to run for: {bullet}"
        lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if not lines:
            bad.append(f"MISSING: {bullet}")
        bad.extend(line for line in lines if line != "sits under: ## Unreleased")
    if bad:
        return "placement: " + " | ".join(bad[:5])
    return None


def gate_single_unreleased(scratch):
    proc = run(["grep", "-c", "^## Unreleased$", "CHANGELOG.md"], cwd=scratch)
    count = proc.stdout.strip() or "0"
    if count != "1":
        return f"{count} `## Unreleased` headings — a duplicate is invisible to probes 2 and 4"
    return None


def gate_size(pr, cfg):
    limit = cfg.get("max_additions") or 0
    file_limit = cfg.get("max_changed_files") or 0
    additions = pr.get("additions") or 0
    files = pr.get("changedFiles") or 0
    if limit and additions > limit:
        return f"size: {additions} additions over the {limit} park limit"
    if file_limit and files > file_limit:
        return f"size: {files} files over the {file_limit} park limit"
    return None


def gate_noop(scratch):
    out = git(scratch, "rev-list", "--count", "origin/master..HEAD").stdout.strip()
    if out == "0":
        return "rebase produced no commits ahead of master"
    return None


# ------------------------------------------------------------------------------ one PR


def bundle_dir():
    return (os.environ.get(BUNDLE_DIR_ENV) or "").strip()


def kubectl_command():
    return (os.environ.get(KUBECTL_ENV) or "").strip()


def reader_pod():
    return (os.environ.get(READER_POD_ENV) or "").strip()


def operator_workspaces():
    return (os.environ.get(OPERATOR_WORKSPACES_ENV) or "").strip()


def operator_path(cfg):
    """The operator's checkout for this repo — the path the line is run from.

    Derived from configuration rather than from `cfg["path"]`: that one is the pod's
    clone, `expanduser`d against a `HOME` the deployment sets to the transfer volume, so
    reusing it would emit `/workspace/...` and re-create the defect.
    """
    return os.path.join(operator_workspaces(), cfg["repo"].rsplit("/", 1)[-1])


def transfer_refusal():
    """Why no transfer can be published, or None when one can.

    A missing deployment fact refuses the row rather than emitting a line that cannot
    work: the line names the operator's `kubectl` wrapper and a pod that has to exist,
    and a line that fails after it is pasted is indistinguishable from a broken rebase.
    """
    missing = [
        name
        for name, value in (
            (BUNDLE_DIR_ENV, bundle_dir()),
            (KUBECTL_ENV, kubectl_command()),
            (READER_POD_ENV, reader_pod()),
            (OPERATOR_WORKSPACES_ENV, operator_workspaces()),
        )
        if not value
    ]
    if missing:
        return "transfer is not configured — " + ", ".join(missing) + " unset"
    return None


def bundle_path(number):
    return os.path.join(bundle_dir(), f"pr-{number}.bundle")


def publish_bundle(repo_path, number):
    """Write the rebased commit to the PVC as a range bundle; return its path.

    A range (`origin/master..<scratch ref>`) rather than a full bundle: the operator's
    checkout already has `origin/master`, so this carries only the commits the push
    needs, and a full bundle of a long branch would be megabytes for nothing.
    """
    path = bundle_path(number)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    proc = git(
        repo_path, "bundle", "create", path,
        f"origin/master..{SCRATCH_REF}/pr/{number}",
    )
    if proc.returncode != 0:
        raise Refusal(f"could not write the transfer bundle: {proc.stderr.strip()}")
    return path


def push_line(operator_path, branch, head_sha, number, bundle):
    """The operator's one line — copy the bundle off the reader pod, fetch it, push.

    Every interpolated value is shell-quoted. `branch` comes from the PR, and this line
    is a shell command the operator pastes — an unquoted branch name is command
    injection regardless of where it came from.

    Three commands chained with `&&`, not three steps: the operator pastes one line and
    does nothing else, and a copy that succeeded while the fetch failed must stop the
    push rather than leave a stale ref to push. `operator_path` is the Mac's checkout —
    never the pod's, which is the defect the transfer channel exists to remove.
    """
    local = shlex.quote(LOCAL_BUNDLE.format(number=number))
    copy = (
        f"{shlex.quote(kubectl_command())} cp "
        f"{shlex.quote(f'{reader_pod()}:{bundle}')} {local}"
    )
    ref = f"{SCRATCH_REF}/pr/{number}"
    # The `+` is load-bearing: the Mac's scratch ref survives between lines, so a
    # re-sweep fetches a newer commit onto an older ref, which a plain refspec refuses.
    fetch = (
        f"git -C {shlex.quote(operator_path)} fetch {local} "
        f"{shlex.quote(f'+{ref}:{ref}')}"
    )
    lease = shlex.quote(f"refs/heads/{branch}:{head_sha}")
    refspec = shlex.quote(f"{ref}:refs/heads/{branch}")
    push = (
        f"git -C {shlex.quote(operator_path)} push "
        f"--force-with-lease={lease} origin {refspec}"
    )
    return f"{copy} && {fetch} && {push}"


# ---------------------------------------------------------------------------- the board


def board_store():
    """The store base URL, or "" when no board is configured."""
    return (os.environ.get(BOARD_STORE_ENV) or "").strip().rstrip("/")


def board_token():
    return (os.environ.get(BOARD_TOKEN_ENV) or "").strip()


def board_host(store):
    try:
        return (urllib.parse.urlsplit(store).hostname or "").strip("[]")
    except ValueError:
        return ""


def board_refusal(store, token):
    """Why this store must not be posted to, or None when it may.

    The guard exists so that *absent* and *safe* coincide: an unauthenticated
    non-loopback store lets anything that can reach the port read every card and release
    every gate, which is strictly worse than the gap this closes. Loopback with no token
    is the legitimate local case.
    """
    if not store:
        return f"{BOARD_STORE_ENV} is unset — no board write"
    if token or board_host(store) in BOARD_LOCAL_HOSTS:
        return None
    return (
        f"REFUSED: {store} is not loopback and {BOARD_TOKEN_ENV} is unset — "
        "an unauthenticated remote store is readable by anything that can reach it"
    )


def board_payload(row):
    """The card's own text: what is behind, and the exact line to run.

    The line is carried verbatim because it is the operator's next action, and the task
    that added this path requires the card to contain it exactly. The schema's "no commit
    SHAs in the sentence" rule governs the prose — the SHA here is the lease pin inside a
    command, and removing it would make the command unsafe to paste.
    """
    return (
        f"{row['repo']}#{row['pr']} ({row['branch']}) is behind master and rebases "
        f"cleanly. Run this line to update the branch:\n\n{row['push_line']}"
    )


def board_context():
    return (
        "The agent rebases every behind-master open PR in a scratch worktree, verifies "
        "the result, and never pushes. The line is this run's own output, reproduced "
        "verbatim; it names the clone path the run used, so compare it with your own "
        "checkout before running it."
    )


def board_body(row, expires_at):
    """The push body for one row. Pure — the caller supplies the deadline."""
    return {
        "producer_id": BOARD_PRODUCER_ID,
        "producer_kind": BOARD_PRODUCER_KIND,
        "provenance_class": "explicit",
        "liveness_ref": BOARD_LIVENESS_REF,
        # Per PR, so a sweep that keeps finding the same branch refreshes ONE card. The
        # store compares this within a live producer, which is why the liveness model
        # above has to read live for a pod.
        "dedup_key": f"{BOARD_PRODUCER_ID}:{row['repo']}#{row['pr']}",
        "answer_mechanism": BOARD_ANSWER_MECHANISM,
        "interrupt_class": BOARD_INTERRUPT_CLASS,
        "payload": board_payload(row),
        "context": board_context(),
        "expires_at": expires_at,
    }


def post_push_line(row, now=None):
    """Post one row's push line; return `(item_id, error)`.

    Never raises. A board that is down must not turn a rebase verdict into an ERROR, and
    the run record still carries the line — the error is returned so the record can name
    it and the operator can tell "no card" from "no push line".
    """
    store = board_store()
    refusal = board_refusal(store, board_token())
    if refusal:
        return None, refusal
    deadline = (now or datetime.now(timezone.utc)) + timedelta(
        seconds=BOARD_CARD_TTL_SECONDS
    )
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    token = board_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        store + BOARD_PATH,
        data=json.dumps(board_body(row, deadline.isoformat())).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=BOARD_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as error:
        # The store's own explanation is carried through: a bare "returned 400" names no
        # field, and a rejected declaration is rejected identically forever.
        detail = error.read().decode("utf-8", "replace")
        return None, f"board returned {error.code} for {row['repo']}#{row['pr']}: {detail}"
    # ⚠️ Three clauses, not one: a timeout and a malformed body are NOT "unreachable", and
    # reporting them that way sends a reader to the network for a store that answered.
    except TimeoutError:
        return None, f"board did not answer within {BOARD_TIMEOUT_SECONDS}s at {store}"
    except urllib.error.URLError as error:
        return None, f"board unreachable at {store}: {error}"
    except ValueError as error:
        return None, f"board returned a malformed body for {row['repo']}#{row['pr']}: {error}"
    except Exception as error:  # noqa: BLE001 — a board must never fail the sweep
        # `http.client.IncompleteRead` and friends are not OSError/ValueError, and a
        # truncated response that escaped would abort the sweep before `write_state` —
        # losing the very record this placement exists to protect.
        return None, f"board post failed for {row['repo']}#{row['pr']}: {error!r}"
    return body.get("item_id"), None


def board_post_row(row):
    """Post one row's push line and record the outcome on the row.

    Owns its own catch so that nothing the board can do reaches `process_pr`'s verdict.
    `post_push_line` already promises not to raise; this keeps that promise true even if
    it is ever broken, because the cost of the alternative is a sweep that lost its record
    to one truncated HTTP response.
    """
    try:
        item_id, board_error = post_push_line(row)
    except Exception as error:  # noqa: BLE001 — see the docstring
        item_id = None
        board_error = f"board post failed for {row['repo']}#{row['pr']}: {error!r}"
    row["board_item_id"] = item_id
    if board_error:
        row["board_error"] = board_error
        print(f"board: {board_error}", file=sys.stderr)
    return row


def process_pr(cfg, pr, keep_scratch=False):
    repo_path = cfg["path"]
    number = pr["number"]
    branch = pr["headRefName"]
    row = {
        "repo": cfg["repo"],
        "pr": number,
        "branch": branch,
        "title": pr.get("title"),
        "review": pr.get("reviewDecision"),
        "draft": bool(pr.get("isDraft")),
        "verdict": None,
        "reason": None,
        "behind": None,
        "push_line": None,
        "head_sha": None,
        "new_sha": None,
        "scratch": None,
        # `None` on every row that is not a PUSH: the key must be present so a reader
        # can tell "no transfer" from "a build that never learned the field".
        "transfer": None,
        "board_item_id": None,
        "board_error": None,
    }

    if not os.path.isdir(repo_path):
        row["verdict"] = "ERROR"
        row["reason"] = f"repo path does not exist: {repo_path}"
        return row

    head_repo = (pr.get("headRepository") or {}).get("nameWithOwner")
    if head_repo and head_repo != cfg["repo"]:
        row["verdict"] = "REFUSED"
        row["reason"] = (
            f"PR head lives in {head_repo}, not {cfg['repo']} — the push line would "
            "target the wrong remote, so a fork PR is not a rebase candidate"
        )
        return row

    changed = changed_paths(cfg, number)
    if changed is None:
        row["verdict"] = "ERROR"
        row["reason"] = "could not read the PR's changed paths — prod-pin check unavailable"
        return row

    pin = prod_pin_reason(pr, changed)
    if pin:
        row["verdict"] = "REFUSED"
        row["reason"] = f"prod-pin PR — the operator's approval, not a rebase candidate ({pin})"
        return row

    # Before the rebase, not after: a deployment with no transfer channel would otherwise
    # spend a worktree and a rebase to discover it cannot hand the result over.
    unconfigured = transfer_refusal()
    if unconfigured:
        row["verdict"] = "REFUSED"
        row["reason"] = unconfigured
        return row

    scratch = None
    try:
        head_sha = fetch_head(repo_path, number)
        row["head_sha"] = head_sha
        row["behind"] = int(
            git(repo_path, "rev-list", "--count", f"{head_sha}..origin/master",
                check=True).stdout.strip() or 0
        )

        scratch = tempfile.mkdtemp(
            prefix=f"rebase-agent-{cfg['repo'].replace('/', '-')}-{number}-"
        )
        added = git(repo_path, "worktree", "add", "--detach", scratch, head_sha)
        if added.returncode != 0:
            raise Refusal(f"could not create scratch worktree: {added.stderr.strip()}")

        base_sha = rebase_onto_master(scratch, repo_path, head_sha)
        bullets, _ = changelog_delta(repo_path, base_sha, head_sha)

        failures = [
            gate_linear(scratch),
            gate_heading_guard(scratch),
            gate_dropped_bullet(scratch),
            gate_placement(scratch, bullets),
            gate_single_unreleased(scratch),
            gate_size(pr, cfg),
            gate_noop(scratch),
        ]
        failures = [failure for failure in failures if failure]
        if failures:
            raise Refusal("gate failed — " + " ;; ".join(failures))

        new_sha = git(scratch, "rev-parse", "HEAD", check=True).stdout.strip()
        row["new_sha"] = new_sha
        git(repo_path, "update-ref", f"{SCRATCH_REF}/pr/{number}", new_sha, check=True)

        row["verdict"] = "PUSH"
        bundle = publish_bundle(repo_path, number)
        row["transfer"] = {"kind": "pvc-bundle", "target": f"{reader_pod()}:{bundle}"}
        row["push_line"] = push_line(
            operator_path(cfg), branch, head_sha, number, bundle
        )
    except Refusal as refusal:
        row["verdict"] = "REFUSED"
        row["reason"] = str(refusal)
    except Exception as error:  # noqa: BLE001 — one PR's failure must not kill the sweep
        row["verdict"] = "ERROR"
        row["reason"] = str(error)
    finally:
        if scratch and os.path.isdir(scratch) and not keep_scratch:
            git(repo_path, "worktree", "remove", "--force", scratch)
            shutil.rmtree(scratch, ignore_errors=True)
            git(repo_path, "worktree", "prune")
        if keep_scratch:
            row["scratch"] = scratch

    # The board write is deliberately OUTSIDE the try above: a store that is down must not
    # reach the `except Exception` arm and turn a good rebase into an ERROR verdict.
    if row["verdict"] == "PUSH" and board_store():
        board_post_row(row)
    return row


# ------------------------------------------------------------------------------- main


def render(rows):
    if not rows:
        print("no open PR is behind master — nothing to rebase")
        return
    print(f"{'REPO':32} {'PR':>5} {'BEHIND':>6}  {'VERDICT':8} BRANCH")
    for row in rows:
        behind = row["behind"] if row["behind"] is not None else "-"
        print(
            f"{row['repo']:32} {row['pr']:>5} {behind:>6}  "
            f"{row['verdict']:8} {row['branch']}"
        )
        if row["reason"]:
            print(f"{'':32} {'':>5} {'':>6}  {'':8} └─ {row['reason']}")
        if row["push_line"]:
            print(f"{'':32} {'':>5} {'':>6}  {'':8} └─ {row['push_line']}")
        if row.get("board_item_id"):
            print(f"{'':32} {'':>5} {'':>6}  {'':8} └─ board: {row['board_item_id']}")
        if row.get("board_error"):
            print(f"{'':32} {'':>5} {'':>6}  {'':8} └─ board: {row['board_error']}")
    pushes = sum(1 for row in rows if row["verdict"] == "PUSH")
    refused = sum(1 for row in rows if row["verdict"] != "PUSH")
    print()
    print(
        f"{pushes} push line(s), {refused} refused/failed. "
        "No push was executed by this agent — every line is the operator's."
    )


def write_state(rows, errors, trigger, state_dir=None):
    """Write the per-run state and return `(path, payload)`.

    `trigger` is part of the payload rather than only of the printed line: the state
    file is what a later run reads to tell a scheduled sweep from a hand-started one.
    """
    directory = state_dir or STATE_DIR
    os.makedirs(directory, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trigger": trigger,
        "errors": errors,
        "rows": rows,
    }
    for name in (f"{stamp}.json", "latest.json"):
        with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
    prune_state(directory)
    return os.path.join(directory, f"{stamp}.json"), payload


def record_line(payload, path):
    """One machine-readable line naming the run, its trigger and every row.

    Single-line and fixed-prefix on purpose: the readers are `kubectl logs` and `grep`
    against a pod that no longer exists, and a pretty-printed blob is unusable through
    either.
    """
    return RECORD_PREFIX + json.dumps(
        {
            "agent": "rebase-agent",
            "generated_at": payload["generated_at"],
            "trigger": payload["trigger"],
            "state": path,
            "errors": payload["errors"],
            "rows": payload["rows"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def prune_state(directory=None):
    """Keep the newest STATE_KEEP runs — the loop writes one file per 900 s."""
    directory = directory or STATE_DIR
    try:
        runs = sorted(
            (os.path.join(directory, name) for name in os.listdir(directory)
             if name.endswith(".json") and name != "latest.json"),
            key=os.path.getmtime,
            reverse=True,
        )
    except OSError:
        return
    for path in runs[STATE_KEEP:]:
        try:
            os.remove(path)
        except OSError:
            pass


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", action="append", default=None,
                        help="restrict to one owner/repo (repeatable)")
    parser.add_argument("--only", type=int, default=None, help="process one PR number")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--keep-scratch", action="store_true",
                        help="leave the scratch worktree in place for inspection")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--trigger", choices=TRIGGERS, default="manual",
                        help="what started this run, written into the record "
                             "(default: manual)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    global STATE_DIR
    if args.state_dir:
        STATE_DIR = args.state_dir

    cfgs = [cfg for cfg in DEFAULT_REPOS if not args.repo or cfg["repo"] in args.repo]
    # A --repo outside the set filters cfgs to empty, and an empty row list used to
    # print the clean-queue line — reading as "nothing is behind" when the truth is
    # "this repo was never examined". Measured 2026-11-13 on bborbe/git-rest#81 and
    # bborbe/agent-task-executor#161, both CONFLICTING and both reported clean.
    known = {cfg["repo"] for cfg in DEFAULT_REPOS}
    unknown_repos = [repo for repo in (args.repo or []) if repo not in known]

    rows, errors = [], []
    for cfg in cfgs:
        try:
            prs = list_open_prs(cfg)
            fetch_master(cfg["path"])
        except Exception as error:  # noqa: BLE001
            errors.append(f"{cfg['repo']}: {error}")
            rows.append({
                "repo": cfg["repo"], "pr": "-", "branch": "-", "verdict": "UNKNOWN",
                "reason": f"queue could not be read: {error}", "behind": None,
                "push_line": None, "board_item_id": None, "board_error": None,
            })
            continue
        for pr in prs:
            if args.only is not None and pr["number"] != args.only:
                continue
            if not needs_rebase(pr):
                continue
            rows.append(process_pr(cfg, pr, keep_scratch=args.keep_scratch))

    # Once per run, and only when there is a push line to lose: a clean queue has nothing
    # to deliver, so warning on it would put a line in the log every 15 minutes for a
    # state that is already correct.
    if not board_store() and any(row["verdict"] == "PUSH" for row in rows):
        print(
            f"board: {BOARD_STORE_ENV} is unset — push lines stay in the run record only",
            file=sys.stderr,
        )

    path, payload = write_state(rows, errors, args.trigger)
    if args.json:
        print(json.dumps({
            "state": path,
            "trigger": args.trigger,
            "errors": errors,
            "repos_examined": [cfg["repo"] for cfg in cfgs],
            "repos_not_in_set": unknown_repos,
            "rows": rows,
        }, indent=2))
    else:
        print(record_line(payload, path))
        if unknown_repos:
            print(
                "repo not in the rebase set: " + ", ".join(unknown_repos)
                + " — the set is " + ", ".join(sorted(known))
                + " (not examined, not clean)"
            )
        if cfgs:
            render(rows)
        print(f"\nstate: {path}")

    if errors:
        return 2
    if unknown_repos and not cfgs:
        # Nothing configured was examined: this is not a clean queue.
        return 2
    return 1 if any(row["verdict"] != "PUSH" for row in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
