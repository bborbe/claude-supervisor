#!/usr/bin/env python3
"""Which goal a session belongs to, read from the vault — the ownership input the spawn edge cannot give.

`gate-owner-filter.py` decides ownership from the **spawn edge plus liveness**: a gate is
dropped only when its spawner is a live manager, the gated session is itself a live manager,
or a live manager holds an ownership claim. Every one of those inputs is a *live* store, and
every one of them fails open — an unreadable ledger, a dead spawner, an unresolved
`parent_session` and a registry miss all land on `EMIT`. So a manager whose goal is one of
thirty on the machine is handed gates raised by **other goals' workers** whenever that live
chain does not resolve, and the filter has no vocabulary to say whose they are: it has no
notion of goals or tasks at all.

Measured 2026-10-29 on goal manager `49b471f1` (goal *Managers Unblock Their Own Workers and
Approve Routine Permissions Within Policy*): **five foreign gates in one afternoon, across
five sessions and four goals** — panes 269/`695c311f`, 260/`0eacc5e4`, 272/`2767ba96`,
278/`0b7d28a7`, 279/`51e78933`. None of the five is in that goal's tracked set. The same five
read `peer-manager` when the spawn edge *does* resolve, so the filter's answer for one gate
depended on the liveness of a manager that had nothing to do with it.

This module answers the durable question instead: **which page owns this session, and does
that page belong to the asking manager's goal?** The vault is the source, because the stamps
are written at spawn and outlive the manager that wrote them.

⚠️ **It is deliberately a second, INDEPENDENT input, not a replacement for the spawn edge.**
The spawn edge answers "who opened this" and the vault answers "whose work is this". They
disagree legitimately — a handover, a predecessor manager, a worker spawned by a peer for a
task that belongs to this goal — and the caller keeps the spawn edge's answer for every case
this module cannot resolve.

## The four reads

  1. `claude_session_id:` on a **goal** page — the goal manager's own session. Its scope is
     that goal's `# Tasks` list. This is how an operator-opened manager is identified at all:
     it was never spawned, so the spawn ledger has no record of it.
  2. `claude_session_id:` on a **task** page — the task's owning session.
  3. `- session_id:` entries under `metrics_sessions:` on a task page — every session that
     WORKED that task. ⚠️ Not optional: one of the five measured cases (`0eacc5e4`, pane 260)
     resolves through this field only, so a reader keyed on `claude_session_id` alone would
     call it unattributed and keep emitting it.
  4. `goals:` on a task page — the goal that owns the task, which is the name reported as
     `foreign-goal:<goal>`.

A task is **in scope** when it is the asking session's own task, is listed in one of its
goals' `# Tasks` list, or declares one of those goals itself. The third arm is a deliberate
widening: a task whose `goals:` names this goal but which the goal page forgot to list is
still this manager's work, and the failure direction of NOT widening is the dangerous one — an
own gate excluded from the feed the manager exists to see.

## Fail open, and say so

Unresolvable is not foreign. A session with no page, a session stamped on two pages, an asking
session with no page of its own, and a vault list that cannot be read at all answer **"not
foreign"** — the gate is kept exactly as it was before this module existed, and the caller
carries the reason on stderr. That is the same asymmetry every hop in `gate-owner-filter.py`
takes: one wasted wake against a fleet-wide silence. ⚠️ A `vault-cli` that cannot answer
falls back to probing every vault directory under `VAULTS_ROOT` (widening, never guessing)
before it reports a reason.

⚠️ **`owner-unknown` is the one exclusion that names nobody**, and it is still an exclusion by
contract: a task that declares no goal is nobody's tracked work,
so no manager is its owner and this watcher has no channel to answer it either. It leaves
THIS manager's feed and is still carried in `--explain --json` — never dropped from the row
set, because a row that disappears from a sweep is indistinguishable from a row that got
fixed (`65 Runbooks/Manager Session.md` § Sweep output). ⚠️ **It is also still an open item on
the attention board**, which is the channel that does not depend on any manager's scope: the
exclusion removes a WAKE, never the gate.

⚠️ **A TOPIC manager is unresolved today, and that is a named limitation rather than an
oversight.** `--self` is matched against task pages (`claude_session_id`) and goal pages
(`claude_session_id`, or the retired `manager_session_id` as a fallback); a topic page
(`23 Topics/`) is not read, so a topic manager resolves
no scope, prints the `SCOPE  unresolved:` line, and excludes nothing — its behaviour is exactly
what shipped before this module existed. Widening it to topics is a separate question (a
topic's scope is its member goals, not a `# Tasks` list) and is deliberately not guessed here.

## Cost, measured

One `load()` walks every configured vault's task and goal folders — **35 159 task pages and
894 goal pages, ~7 s** on 2026-10-29 (the four largest trees are `private-agent/tasks`,
`private-personal/25 Tasks`, `seibert-agent/tasks` and `seibert-brogrammers/25 Tasks`). The
caller reads it **once per arm**, never per poll: a page stamped mid-arm is simply absent from
the map until the next arm, which reads as unattributed and KEEPS the gate — the same
conservative residual `gate-owner-filter.py` already documents for its event-log read.

## Usage

    session-scope.py --self <session-id> [--vault <path> ...] [--session <id> ...] [--json]

`gate-owner-filter.py` imports `load()` through `importlib`, the same way it reaches
`spawn-ledger.py`.
"""
import glob
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass

#: Vault folder names that hold tasks and goals. Both generations are listed so a vault left
#: on the pre-2026-09-13 names (`24 Tasks` / `23 Goals`) still resolves, exactly as
#: `fleet-sessions.py`'s `PROBE_DIRS` does.
PROBE_TASKS = ("25 Tasks", "24 Tasks", "tasks")
PROBE_GOALS = ("24 Goals", "23 Goals", "goals")

#: Obsidian's vault root. `SUPERVISOR_VAULTS_ROOT` first, for the same reason the session
#: registry's override exists: a reader pointed at a different root must not silently read
#: this machine's vaults.
VAULTS_ROOT = os.environ.get("SUPERVISOR_VAULTS_ROOT") or os.path.expanduser(
    "~/Documents/Obsidian"
)

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_CLAUDE_STAMP = re.compile(r'^claude_session_id:\s*["\']?(' + _UUID + r')', re.M)
_MANAGER_STAMP = re.compile(r'^manager_session_id:\s*["\']?(' + _UUID + r')', re.M)
_SESSION_ENTRY = re.compile(r'^\s*-\s+session_id:\s*["\']?(' + _UUID + r')', re.M)
#: ⚠️ **The trailing newline is OPTIONAL, and that is load-bearing.** `_frontmatter()` returns
#: `text[3:end]`, which drops the closing delimiter's newline — so a `goals:` block that is the
#: LAST frontmatter key ends mid-line. Requiring `\n` made that block match ZERO items and
#: `_goals()` return `[]` with no error at all: a foreign page then named no owner
#: (`owner-unknown` instead of `foreign-goal:<goal>`), and a manager whose own task declares the
#: goal never widened its scope, so its own workers' gates were excluded from the feed it exists
#: to see. Only the last-key shape regressed — any page carrying a key after the block matched
#: exactly as before, which is why every page in the vault's own frontmatter ordering missed it.
_GOALS_BLOCK = re.compile(r"^goals:\s*\n((?:[ \t]+-[^\n]*\n?)*)", re.M)
_WIKILINK = re.compile(r"\[\[([^\]|#]+)")
_TASKS_HEADING = re.compile(r"^# Tasks\s*$", re.M)
_NEXT_HEADING = re.compile(r"^# ", re.M)

#: How much of a TASK page to read before giving up on finding the frontmatter's closing
#: delimiter. Its frontmatter is all this module needs, and task pages carry long `# Progress`
#: tails. Measured 2026-10-29: 35 000 task pages across the configured vaults, so the head is
#: read first and the whole page only when the frontmatter does not close inside it — a
#: truncation loses stamps, and a lost stamp only ever KEEPS a gate (the fail-open direction).
TASK_HEAD_BYTES = 1200


def _frontmatter(text):
    """The YAML block's text, or "" when the page has none.

    Scopes the stamp regexes to frontmatter: a `- session_id:` line quoted in a task's own
    `# Progress` prose must never be read as a stamp. `fleet-sessions.py` takes the same
    precaution by reading `text[:600]`; this reads the real delimiter so a task with many
    metrics sessions is not truncated mid-block.
    """
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return text[3:end] if end > 0 else ""


def _goals(front):
    """The `[[...]]` names under `goals:`, in file order."""
    match = _GOALS_BLOCK.search(front)
    if not match:
        return []
    return [name.strip() for name in _WIKILINK.findall(match.group(1))]


def _task_links(text):
    """Every `[[...]]` named in a goal page's `# Tasks` list, first link per bullet.

    ⚠️ **The FIRST link on the line, not every link.** A bullet's shape is
    `- [ ] <prose> (→ SC1) — [[The Task]]`, so the first link is the task; a bullet carrying
    two links (a task plus a cross-reference) would otherwise drag the second page into the
    scope, and that failure direction is an own gate excluded.
    """
    match = _TASKS_HEADING.search(text)
    if not match:
        return []
    rest = text[match.end():]
    nxt = _NEXT_HEADING.search(rest)
    section = rest[: nxt.start()] if nxt else rest
    out = []
    for line in section.splitlines():
        if not line.lstrip().startswith("-"):
            continue
        links = _WIKILINK.findall(line)
        if links:
            out.append(links[0].strip())
    return out


def _probe(root, names):
    """Existing probe folders under `root`, as absolute paths."""
    return [os.path.join(root, name) for name in names
            if os.path.isdir(os.path.join(root, name))]


def _dirs_under(root):
    """`(task_dirs, goal_dirs)` for an explicitly named vault root."""
    return _probe(root, PROBE_TASKS), _probe(root, PROBE_GOALS)


def _vault_dirs(explicit=None):
    """`[(task_dirs, goal_dirs)]` per vault, plus a degradation reason.

    `explicit` is a list of vault ROOTS (a fixture's stub vault, or an operator override).
    When absent the list comes from `vault-cli`'s own config, which is the single source of
    truth for where each vault keeps its tasks and goals; a vault missing from the config
    falls back to the probe names. A failure returns `([], reason)` and the caller keeps every
    gate — never an empty scope that would read as "nothing is mine".
    """
    if explicit:
        out = []
        for root in explicit:
            root = os.path.expanduser(root)
            if not os.path.isdir(root):
                return [], f"vault path does not exist: {root}"
            out.append(_dirs_under(root))
        return out, ""
    try:
        res = subprocess.run(
            ["vault-cli", "--output", "json", "config", "list"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _probe_root(f"vault-cli config list failed: {type(exc).__name__}")
    if res.returncode != 0:
        return _probe_root(f"vault-cli config list exited {res.returncode}")
    try:
        payload = json.loads(res.stdout)
    except ValueError:
        return _probe_root("vault-cli config list returned unparseable JSON")
    if not isinstance(payload, list):
        return _probe_root("vault-cli config list returned something other than a list")
    out = []
    for vault in payload:
        if not isinstance(vault, dict):
            continue
        path = os.path.expanduser(vault.get("path") or "")
        if not path or not os.path.isdir(path):
            continue
        tasks = [os.path.join(path, vault["tasks_dir"])] if vault.get("tasks_dir") else []
        goals = [os.path.join(path, vault["goals_dir"])] if vault.get("goals_dir") else []
        out.append((tasks + _probe(path, PROBE_TASKS), goals + _probe(path, PROBE_GOALS)))
    if not out:
        return _probe_root("no configured vault is readable")
    return out, ""


def _probe_root(reason):
    """The fallback: every vault directory under `VAULTS_ROOT`, probed for the folder names.

    ⚠️ **Widening, not guessing, and it is the same fallback `fleet-sessions.py` takes.** A
    `vault-cli` that cannot answer must not turn into "no vault" — that would read as "nothing
    is mine" and exclude on a broken instrument, the failure this module fails open to avoid.
    Probing can only find MORE vaults than the config would, which can only make an own gate
    more likely to stay own. The reason is still reported when the probe finds nothing, so a
    genuinely broken environment is not silent.
    """
    out = []
    try:
        entries = sorted(os.listdir(VAULTS_ROOT))
    except OSError:
        entries = []
    for name in entries:
        path = os.path.join(VAULTS_ROOT, name)
        if not os.path.isdir(path):
            continue
        tasks, goals = _dirs_under(path)
        if tasks or goals:
            out.append((tasks, goals))
    if not out:
        return [], reason
    return out, ""


@dataclass(slots=True)
class Entry:
    """One page a session id is stamped on.

    A value record, not a service: `kind` is `"task"` or `"goal"`, and `goals` is the page's
    own `goals:` list (a goal page carries its own title).
    """

    title: str
    kind: str
    goals: list


@dataclass
class Scope:
    """The asking manager's tracked set, and what a session's page says about it.

    `sessions` is `{session_id: [Entry, ...]}`, `goals` is `{goal title: set(task titles)}`,
    `own_titles` is every title in this manager's scope, `self_goals` is the asking session's
    goals (empty when it resolves to a task), and `reason` is `""` when scoped and otherwise
    says why not.
    """

    sessions: dict
    goals: dict
    own_titles: set
    self_goals: list
    reason: str

    @property
    def resolved(self):
        """True only when the asking session has a page this module could scope.

        ⚠️ **Unresolved means "keep everything", never "nothing is mine".** A manager opened
        outside the vault's stamps (a bare session, a worker, a test harness) would otherwise
        have every gate excluded as foreign — a fleet-wide silence produced by a missing page.
        """
        return bool(self.own_titles)

    def exclusion(self, session_id, self_id):
        """`(exclude?, reason)` for one gated session.

        `reason` is `foreign-goal:<goal>` when the owning page declares one, and
        `owner-unknown` when it declares none. Both are exclusions; neither is a drop from the
        row set — the caller keeps the row and marks it.
        """
        if not self.resolved or not session_id or session_id == self_id:
            return False, ""
        entries = self.sessions.get(session_id) or []
        titles = {entry.title for entry in entries}
        # No page at all, or the id is stamped on two pages: unattributed. Keeping it is the
        # same fail-open every other ownership input takes.
        if len(titles) != 1:
            return False, ""
        entry = entries[0]
        if entry.title in self.own_titles:
            return False, ""
        goal = entry.goals[0] if entry.goals else None
        if goal:
            return True, f"foreign-goal:{goal}"
        return True, "owner-unknown"


def _read_task(path):
    """A task page's frontmatter-bearing head, widened when the block does not close in it."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            head = handle.read(TASK_HEAD_BYTES)
            if head.startswith("---") and head.find("\n---", 3) < 0:
                head += handle.read()
            return head
    except OSError:
        return None


def _walk(task_dirs, goal_dirs):
    """`(task pages, goal pages)` as `(path, text)` pairs.

    ⚠️ **Directories are de-duplicated by realpath.** A vault whose configured `tasks_dir` is
    itself one of the probe names (`private-agent` configures `tasks`, and `tasks` is also a
    probe name) otherwise contributes the same tree twice, which doubled this walk's page
    count in the first cut.
    """
    tasks, goals, seen = [], [], set()
    for directory in task_dirs:
        real = os.path.realpath(directory)
        if real in seen:
            continue
        seen.add(real)
        for path in glob.glob(os.path.join(directory, "*.md")):
            head = _read_task(path)
            if head is not None:
                tasks.append((path, head))
    seen = set()
    for directory in goal_dirs:
        real = os.path.realpath(directory)
        if real in seen:
            continue
        seen.add(real)
        for path in glob.glob(os.path.join(directory, "*.md")):
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    goals.append((path, handle.read()))
            except OSError:
                continue
    return tasks, goals


def load(self_id, vault_paths=None):
    """Build the `Scope` for `self_id`, or one whose `reason` says why it could not.

    Never raises, and never returns a scope that would exclude on a failed read.
    """
    # ⚠️ Checked BEFORE the walk. A `--pane` diagnostic with no `--self` cannot be scoped,
    # and the walk is ~7 s over 35 000 pages — paying it to learn a fact already known is the
    # one cost this input must not add to a call that never asked for it.
    if not self_id:
        return Scope({}, {}, set(), [], "no --self session id supplied")
    dirs, reason = _vault_dirs(vault_paths)
    if reason:
        return Scope({}, {}, set(), [], reason)

    task_pages, goal_pages = _walk(
        [d for pair in dirs for d in pair[0]],
        [d for pair in dirs for d in pair[1]],
    )
    if not task_pages and not goal_pages:
        return Scope({}, {}, set(), [], "no task or goal page found in any configured vault")

    sessions, goals, managers, declaring = {}, {}, {}, {}

    def stamp(session_id, entry):
        bucket = sessions.setdefault(session_id, [])
        if all(other.title != entry.title for other in bucket):
            bucket.append(entry)

    for path, text in task_pages:
        front = _frontmatter(text)
        if not front:
            continue
        title = os.path.splitext(os.path.basename(path))[0]
        declared = _goals(front)
        entry = Entry(title, "task", declared)
        for goal in declared:
            declaring.setdefault(goal, set()).add(title)
        match = _CLAUDE_STAMP.search(front)
        if match:
            stamp(match.group(1), entry)
        for session_id in _SESSION_ENTRY.findall(front):
            stamp(session_id, entry)

    for path, text in goal_pages:
        title = os.path.splitext(os.path.basename(path))[0]
        # MERGE, never overwrite. `goals` is keyed by page basename and every `private-*` /
        # `seibert-*` vault carries a `24 Goals/…` tree, so a shared basename is routine.
        # Overwriting let the last vault's `# Tasks` replace -- or, with no `# Tasks`
        # heading, WIPE -- the set `own_titles` is built from, so an own task linked only
        # from the first vault's page was EXCLUDEd as foreign. That is the fleet-silence
        # direction this module exists to prevent. `declaring` merges and `managers`
        # appends for the same reason; this walk was the one that did not.
        goals.setdefault(title, set()).update(_task_links(text))
        front = _frontmatter(text)
        if front:
            # `claude_session_id` first, `manager_session_id` second: the writer sets
            # `claude_session_id` without clearing the retired key, so a re-armed legacy
            # page carries BOTH — reading the retired key first would hand back the stale
            # id. The fallback answers only for a page the new writer has not touched yet.
            match = _CLAUDE_STAMP.search(front) or _MANAGER_STAMP.search(front)
            if match:
                managers.setdefault(match.group(1), []).append(title)

    own_titles, self_goals, why = set(), [], ""
    if self_id in managers:
        self_goals = sorted(set(managers[self_id]))
    else:
        entries = sessions.get(self_id) or []
        titles = {entry.title for entry in entries}
        if len(titles) != 1:
            why = (
                f"session {self_id[:8]} is stamped on {len(titles)} pages"
                if titles
                else f"session {self_id[:8]} is stamped on no task or goal page"
            )
        elif entries[0].kind == "goal":
            self_goals = [entries[0].title]
        else:
            own_titles.add(entries[0].title)
            self_goals = list(entries[0].goals)

    if not why:
        for goal in self_goals:
            own_titles.add(goal)
            own_titles |= goals.get(goal, set())
            own_titles |= declaring.get(goal, set())
        if not own_titles:
            why = (
                f"session {self_id[:8]} resolves to "
                f"{self_goals[0] if self_goals else 'a page'} with no tasks"
            )

    return Scope(sessions, goals, own_titles, self_goals, why)


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="Which goal a session belongs to.")
    parser.add_argument("--self", dest="self_id", required=True)
    parser.add_argument(
        "--vault", action="append", default=[],
        help="vault ROOT to read instead of vault-cli's config (repeatable)",
    )
    parser.add_argument("--json", action="store_true", help="dump the scope as JSON")
    parser.add_argument(
        "--session", action="append", default=[],
        help="attribute one session (repeatable)",
    )
    args = parser.parse_args(argv)

    scope = load(args.self_id, args.vault or None)
    if args.json:
        json.dump(
            {
                "self_goals": scope.self_goals,
                "own_titles": sorted(scope.own_titles),
                "reason": scope.reason,
                "sessions": {
                    session_id: [
                        {"title": entry.title, "kind": entry.kind, "goals": entry.goals}
                        for entry in entries
                    ]
                    for session_id, entries in scope.sessions.items()
                },
            },
            sys.stdout, indent=2,
        )
        print()
        return 0

    if not scope.resolved:
        print(f"SCOPE  unresolved: {scope.reason} — every gate emits", file=sys.stderr)
        return 0
    print(
        f"SCOPE  goals: {len(scope.self_goals)}  own titles: {len(scope.own_titles)}  "
        f"sessions: {len(scope.sessions)}"
    )
    for session_id in args.session:
        excluded, reason = scope.exclusion(session_id, args.self_id)
        print(f"{session_id[:8]}  {'excluded ' + reason if excluded else 'own'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
