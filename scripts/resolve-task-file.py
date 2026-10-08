#!/usr/bin/env python3
"""Resolve session task files by name, across every configured vault.

One home for the fleet sweep's **name-based** task-file resolution.
`agents/fleet-sweep-reader.md` step 4 calls this; the rule is not restated there beyond
what a caller needs. The other leg — the `claude_session_id:` stamp — is not here and does
not need to be: `fleet-sessions.py`'s `_walk_task_stamps()` already walks every vault under
the Obsidian root, using `vault_dirs_from_cli()` with `PROBE_DIRS` as its fallback.

**Why this is a script and not a snippet in the agent file.** The rule regressed once
already — a first cut treated any two hits as ambiguous, which broke the task-over-goal
precedence for a name that is a task in one folder and a goal in the *same* vault
(`BRO-22032 MDM Merge of Parties based on names` is exactly that shape). A rule carried as
prose in a markdown file has nothing to run against it, so the cases that caught the
regression lived only in a PR body. Extracted 2026-10-07 so
`scripts/tests/test_resolve_task_file.py` pins them.

**Resolution is tier by tier, never globally:**

  * exactly one `tasks_dir` hit across all vaults  -> that path
  * else, with NO `tasks_dir` hit at all, exactly one `goals_dir` hit -> that path
  * else -> no resolution

An ambiguous `tasks_dir` tier does **not** fall through to `goals_dir`: the condition is
`not tasks` (zero hits), not "tasks had no unique hit". A name that is a task in one vault
and a goal in another resolves to the *task* — the precedence is a property of the name,
not of the vault.

**Never guesses.** Zero hits, or more than one inside the tier reached, resolves nothing;
the colliding paths go to stderr as `AMBIGUOUS <name>\\t<path>`. That is
`docs/subject-resolution.md` § The page test — exact basename, the folder is the
discriminator, both match or neither -> STOP, and every candidate path printed.

⚠️ **A degraded read is announced, never silent.** This is the whole reason the guard below
carries a `DEGRADED` line rather than mirroring `vault_dirs_from_cli()`'s bare `return {}`:
that helper is safe *at its site* only because `fleet-sessions.py` falls back to
`PROBE_DIRS`. Here there is no fallback, so an unannounced `[]` would render every session
unowned — reintroducing, on the failure path, the exact false-*unowned* defect this whole
change exists to remove. A caller that sees `DEGRADED` must render its task-file pass
`UNKNOWN (pass not run)`, never a blank.

**Usage**

    resolve-task-file.py <name>...        # one `<name>\\t<path>` line per name
    resolve-task-file.py --stdin          # same, names read one per line

**Output contract** — one line per name, `<name>` TAB `<value>`, where `<value>` is:

  * a path     — resolved
  * empty      — no resolution (zero hits, or an ambiguity; the candidates are on stderr)
  * `UNKNOWN`  — ⚠️ the vault list could not be read. This is **not** "no resolution": a
                 caller must render its task-file pass `UNKNOWN (pass not run)`, never a
                 blank and never an unowned row. It is carried on **stdout** deliberately,
                 because a caller that reads stdout alone would otherwise see the empty
                 value above and read a broken `vault-cli` as a fleet with no tasks.

stderr carries `DEGRADED <reason>` (mirrored as the `UNKNOWN` values) and
`AMBIGUOUS <name>` TAB `<path>` for every candidate of the tier that was actually reached.

The batch form exists because step 4 is a non-skippable hot path over ~46 live sessions:
enumerating the vaults once per round rather than once per name removes ~46 `vault-cli`
invocations and ~46 interpreter starts per sweep.
"""
import json
import os
import re
import subprocess
import sys
import unicodedata


def vault_dirs():
    """Every configured vault as `{path, tasks_dir, goals_dir}`, plus a degradation reason.

    Returns `(vaults, reason)`. `reason` is `""` on success and a one-line explanation on
    any failure — a non-zero exit, a timeout, a missing binary, an unreadable or
    unparseable config. ⚠️ The loop over the payload is INSIDE the guard, which is the
    defect this function was extracted to fix: a payload that parses to something other
    than a list of dicts (a bare object, or a dict keyed by vault name) otherwise reaches
    `.get()` on a string and raises `AttributeError` straight through a pass the reader
    declares "not skippable, and not partially skippable".
    """
    try:
        res = subprocess.run(
            ["vault-cli", "--output", "json", "config", "list"],
            capture_output=True, text=True, timeout=10,
        )
        if res.returncode != 0:
            return [], f"vault-cli config list exited {res.returncode}"
        payload = json.loads(res.stdout)
        if not isinstance(payload, list):
            return [], f"vault-cli config list returned {type(payload).__name__}, not a list"
        out = []
        seen = set()
        for v in payload:
            if not isinstance(v, dict):
                continue
            path = os.path.expanduser(v.get("path") or "")
            if not path:
                continue
            # Dedupe on the resolved path, as fleet-sessions.py does: a config listing one
            # vault twice — or twice through a symlink — would otherwise make hits() return
            # the same file twice, and resolve() reads two identical hits as a collision.
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            out.append({
                "path": path,
                "tasks_dir": v.get("tasks_dir") or "",
                "goals_dir": v.get("goals_dir") or "",
            })
        if not out:
            return [], "vault-cli config list yielded no usable vault"
        return out, ""
    except subprocess.TimeoutExpired:
        return [], "vault-cli config list timed out after 10s"
    except FileNotFoundError:
        return [], "vault-cli not found on PATH"
    except Exception as exc:                       # noqa: BLE001 — any failure degrades, never raises
        return [], f"vault-cli config list failed: {type(exc).__name__}"


# A registry name is a *truncated* title, and the truncation is long — measured 46
# characters on 2026-10-07 (`⚙ A Renamed Task's Session Becomes Unaddressable` against a
# 108-character title). ⚠️ **`fleet-board.py` and `manager-predispatch.py` carry the same
# constant for the same rule and the three values must agree** — a test pins them
# together, because a drift would make the board, the pre-dispatch gate and this reader
# disagree about whether a name resolves.
_TRUNCATION_MIN_PREFIX = 20


def _norm_session_name(name):
    """Casefolded, decoration-stripped session label.

    A registry name carries a `⚙ ` marker a task title does not, so a raw comparison
    never matches. Mirrors `manager-predispatch.py`'s helper of the same name and job, so
    the readers agree about what a name *is*.
    """
    text = unicodedata.normalize("NFKC", name or "").strip()
    text = re.sub(r"^[^\w(]+", "", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def _matches(basename, stem, prefix):
    """Does a directory entry's basename name this stem?

    Exact by default. ⚠️ **`prefix` is the truncation tier and is reached only when the
    exact tier found nothing** — the registry holds a *truncated* title, so an
    exact-only reader resolves nothing for a long-titled task. The test is a **prefix on
    the stem**, not a substring glob, and it is length-gated so a short role name that
    happens to open a title is not read as a truncation.
    """
    if not prefix:
        return basename == stem
    bare = stem[:-3] if stem.endswith(".md") else stem
    return len(bare) >= _TRUNCATION_MIN_PREFIX and basename.startswith(bare)


def hits(vaults, key, stem, prefix=False):
    """Every regular file `<stem>` under `<vault>/<key>`, across all vaults.

    Exact basename, case-insensitive — never a substring glob, and the folder is the
    discriminator rather than a frontmatter field (`docs/subject-resolution.md`).

    ⚠️ **`prefix=True` is the truncation tier, and it is a different contract from the
    default.** `docs/subject-resolution.md` § The page test states that *"under exact
    matching, two matches inside one folder are impossible by construction"* — true of the
    default and **not** of this tier, where two titles may share a truncated prefix. The
    `AMBIGUOUS` discipline in `resolve` is what keeps the tier honest; the doc's sentence
    describes the exact tier alone.
    """
    found = []
    for v in vaults:
        d = v[key]
        # ⚠️ Refused rather than honoured: an absolute `tasks_dir` makes os.path.join discard
        # the vault path, and a `..` component walks out of it — both probe a directory the
        # vault does not own. No vault-cli config uses either, and searching outside the
        # vault is the failure this guard exists to prevent. A missing dir is skipped — the
        # assistant-* vaults carry neither key, and the vault root must never be probed.
        if not d or os.path.isabs(d) or ".." in d.split(os.sep):
            continue
        full = os.path.join(v["path"], d)
        try:
            entries = os.listdir(full)
        except OSError:
            continue
        found += [os.path.join(full, e) for e in entries
                  if _matches(_norm_session_name(e), stem, prefix)
                  and os.path.isfile(os.path.join(full, e))]
    return found


def resolve(name, vaults):
    """The rule. Returns `(path, ambiguous)`; `path` is `""` when nothing resolves."""
    # A caller may pass the roster name or the filename; both mean the same task, and the
    # suffixed form would otherwise search for `<name>.md.md` and silently resolve nothing.
    stem = _norm_session_name(name)
    if stem.endswith(".md"):
        stem = stem[:-3]
    stem += ".md"
    tasks = hits(vaults, "tasks_dir", stem)
    if len(tasks) == 1:
        return tasks[0], []                # a task beats a same-named goal, in any vault
    goals = hits(vaults, "goals_dir", stem)   # only reached when tasks is empty or ambiguous
    if not tasks:
        if len(goals) == 1:
            return goals[0], []
        if not goals:
            # ⚠️ **Both EXACT tiers found nothing, and only now do the truncation tiers
            # run — tasks still ahead of goals.** Ordering these by folder first would let
            # a *prefix guess* displace an *exact* match in the other folder: with a goal
            # `Some Long Goal Name Here` and a task `Some Long Goal Name Here And Then Some
            # More`, a tasks-first prefix tier resolves the guess and never consults the
            # goal that matches exactly. Certainty before guess, across folders as well as
            # within one — that is the whole of "never guesses".
            tasks = hits(vaults, "tasks_dir", stem, prefix=True)
            if len(tasks) == 1:
                return tasks[0], []
            if not tasks:
                goals = hits(vaults, "goals_dir", stem, prefix=True)
                if len(goals) == 1:
                    return goals[0], []
                return "", goals
            return "", tasks
        return "", goals                   # zero tasks; goals is the tier that was reached
    return "", tasks                       # an ambiguous tasks tier never consults goals_dir,
                                           # so only its own candidates may be announced


def main(argv):
    if argv == ["--stdin"]:
        names = [ln.strip() for ln in sys.stdin if ln.strip()]
    elif argv and not argv[0].startswith("-"):
        names = argv
    else:
        print("usage: resolve-task-file.py <name>... | --stdin", file=sys.stderr)
        return 2

    vaults, reason = vault_dirs()
    if reason:
        print(f"DEGRADED {reason}", file=sys.stderr)
    for name in names:
        # ⚠️ A degraded read is announced ON STDOUT, not only on stderr. A caller that reads
        # stdout alone — a `| cut -f2` pipeline, which is how the sibling command consumes
        # this — would otherwise see an empty path and read it as "no resolution", rendering
        # the session unowned: the very defect this whole change removes, reached through a
        # stream the caller was not watching. `UNKNOWN` is not a path and must never be
        # rendered as one; it means the vault list could not be read.
        if reason:
            print(f"{name}\tUNKNOWN")
            continue
        path, ambiguous = resolve(name, vaults)
        for p in ambiguous:
            print(f"AMBIGUOUS {name}\t{p}", file=sys.stderr)
        print(f"{name}\t{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
