#!/usr/bin/env python3
"""The measured progress window -- did this session move its anchor's closure, from disk.

`docs/session-progress.md` is the design and the single home of the definitions: the
evidence grades, the windows, the ordered verdicts, the claim format. This script is the
Collect and Classify half of it. It reads artifacts, grades them and names a verdict; it
never writes, never messages, and never decides a lever -- that is the drive command's.

WHY FROM DISK. Before this script `/supervisor:worker-drive` took "last progress" from a
`session_digest` the session wrote about itself, so a session going in circles read
healthy. A session's own account is a claim, never an input: it may file one structured
claim (`--claim`), and the script answers whether an artifact in the window backs it.

Sources, all read-only:

- the transcript, `<projects>/*/<session-id>.jsonl` -- by session id, never by cwd, and
  deduplicated by inode (a symlinked project dir matches the same file twice; measured
  2026-10-18). `tool_use` is read from `assistant` records only and `tool_result` from
  `user` records only: `api-request-blob` records repeat both.
- the anchor task file's history in the vault repo that holds it (obsidian-git
  autocommits a tick with nothing else), plus its working-tree state when `--until` is now.
- the git roots the session wrote to, derived from Edit / Write / NotebookEdit paths with
  `git rev-parse --show-toplevel` -- a record's `cwd` is the launch dir, never the worktree.
- the session registry, `<sessions>/<pid>.json`, for liveness and process start only.
  A `busy <-> idle` flip there is activity, never movement.
- the coverage agent's rows (`--coverage`), the one home of a box's evidence rating.

Usage:
  progress-window.py --session <id> --anchor <task file> [--since <ISO>] [--until <ISO>]
                     [--coverage <file>] [--claim <json>] [--json] [--now <ISO>]

`--since` defaults to `last_run` in `<state>/drive-<session>.json`, else `--until` - 15 min,
and is capped at 60 min before `--until` whatever it says. `--until` defaults to now. Both
are echoed in the JSON, with the coverage path + hash and the claim, so a run replays.
`--now` exists so the clock is testable; the working tree is read only when `--until` is
within two minutes of it, because the tree shows now, not a past window's end.

Roots: `SUPERVISOR_PROJECTS_DIR`, `SUPERVISOR_SESSIONS_DIR` (the server's own, see
`server/config.mjs`) and `SUPERVISOR_STATE_DIR`, each defaulting under `~/.claude`.

Exit: 0 a verdict / 2 UNKNOWN -- a source is unreadable, or the session id is known to
neither the registry nor any transcript; the source is named on stderr. UNKNOWN is never
read as a verdict about the session: it is a read that failed.
"""
import argparse
import datetime
import functools
import glob
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys

OK = 0
UNKNOWN_EXIT = 2

#: The short window when no drive ran before (spec § The window).
DEFAULT_WINDOW = datetime.timedelta(minutes=15)
#: The lookback cap: a stale `last_run` never widens the window past this.
LOOKBACK_CAP = datetime.timedelta(minutes=60)
#: The first minutes of a session read C as ORIENTING.
ORIENTATION = datetime.timedelta(minutes=20)
#: `--until` this close to now reads the working tree as the window's end state.
FRESH = datetime.timedelta(minutes=2)

#: TOOL-RUNNING bound: 2x the median completed duration of that tool name, floored.
RUNNING_FLOOR = 120
RUNNING_NO_HISTORY = 600

#: BUSY-NO-MOVE thresholds -- starting values, calibrated from recorded runs.
LOOP_MIN = 3
READ_LOOP_MIN = 10

WRITE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
ASSERTED_KINDS = {"pr", "test", "phase"}
CLAIM_KINDS = {"tick", "commit", "progress-entry"}

BOX_TOKEN = re.compile(r"\b((?:SC|DoD)\d+)\b")
SHA_TOKEN = re.compile(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{7,40}\b")
PATH_TOKEN = re.compile(r"`([^`\s]*/[^`\s]*)`")
CHECKBOX = re.compile(r"^- \[([ xX/])\] (.*)$")
BASH_SINK = re.compile(r"\d*>\s*/dev/null|\d*>&\d")
BASH_WRITE = re.compile(
    r"(^|[;&|(]\s*|\bthen\s+|\bdo\s+)"
    r"(git\s+(commit|push|add|mv|rm|merge|rebase|reset|checkout|switch|tag|stash|worktree\s+add)"
    r"|mv|rm|cp|mkdir|touch|tee|ln|install|npm\s+install|make|vault-cli\s+\S+\s+(set|complete|clear|add|approve))\b"
    r"|>|\bsed\s+-i|\bperl\s+-\w*i"
)
#: The script's own invocation is the caller's trailing call, never the session's work.
SELF = re.compile(r"progress-window\.py\b.*--session")


class Unknown(Exception):
    """A source could not be read. Never a verdict about the session."""


def parse_ts(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.datetime.fromtimestamp(value / 1000.0, datetime.timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    stamp = datetime.datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()
    return stamp.astimezone(datetime.timezone.utc)


def iso(stamp):
    return stamp.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def env_dir(name, *default):
    return os.environ.get(name) or os.path.join(os.path.expanduser("~"), ".claude", *default)


# --- transcript -------------------------------------------------------------------------


def find_transcripts(projects, session):
    """Every transcript for the id, one per inode -- a symlinked dir matches twice."""
    seen, kept = set(), []
    for path in sorted(glob.glob(os.path.join(projects, "*", session + ".jsonl"))):
        try:
            st = os.stat(path)
        except OSError as exc:
            raise Unknown("transcript unreadable: %s (%s)" % (path, exc.strerror))
        key = (st.st_dev, st.st_ino)
        if key not in seen:
            seen.add(key)
            kept.append(os.path.realpath(path))
    return kept


def load_calls(paths):
    """Tool calls keyed by id, and the first timestamp any record carries."""
    calls, first = {}, None
    for path in paths:
        try:
            handle = open(path, encoding="utf-8")
        except OSError as exc:
            raise Unknown("transcript unreadable: %s (%s)" % (path, exc.strerror))
        with handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict) or record.get("isSidechain"):
                    continue
                try:
                    stamp = parse_ts(record.get("timestamp"))
                except ValueError:
                    stamp = None
                if stamp is None:
                    continue
                first = stamp if first is None or stamp < first else first
                kind = record.get("type")
                content = (record.get("message") or {}).get("content")
                if kind not in ("assistant", "user") or not isinstance(content, list):
                    continue
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if kind == "assistant" and item.get("type") == "tool_use" and item.get("id"):
                        calls.setdefault(item["id"], {
                            "name": item.get("name") or "",
                            "input": item.get("input") or {},
                            "ts": stamp,
                        })
                    elif kind == "user" and item.get("type") == "tool_result":
                        call = calls.get(item.get("tool_use_id"))
                        if call is not None and "result_ts" not in call:
                            call["result_ts"] = stamp
                            call["result"] = hashlib.sha256(
                                json.dumps(item.get("content"), sort_keys=True).encode()
                            ).hexdigest()
    return calls, first


def label(call):
    data = call["input"]
    if call["name"] == "Bash":
        return " ".join(str(data.get("command", "")).split())
    return call["name"] + " " + json.dumps(data, sort_keys=True)


def is_self(call):
    return call["name"] == "Bash" and bool(SELF.search(str(call["input"].get("command", ""))))


def is_write(call):
    if call["name"] in WRITE_TOOLS:
        return True
    if call["name"] == "Bash":
        return bool(BASH_WRITE.search(BASH_SINK.sub("", str(call["input"].get("command", "")))))
    return False


def written_paths(call):
    data = call["input"]
    return [data[k] for k in ("file_path", "notebook_path") if isinstance(data.get(k), str)]


# --- registry ---------------------------------------------------------------------------


def find_registry(sessions, session):
    for path in sorted(glob.glob(os.path.join(sessions, "*.json"))):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("sessionId") == session:
            return data
    return None


# --- git --------------------------------------------------------------------------------


def git(root, *args):
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    return proc.returncode, proc.stdout


@functools.lru_cache(maxsize=None)
def toplevel(path):
    here = os.path.abspath(os.path.expanduser(path))
    while here and not os.path.isdir(here):
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent
    code, out = git(here, "rev-parse", "--show-toplevel")
    return os.path.realpath(out.strip()) if code == 0 and out.strip() else None


def show(root, rev, rel):
    code, out = git(root, "show", "%s:%s" % (rev, rel))
    return out if code == 0 else None


def commit_time(root, sha):
    code, out = git(root, "show", "-s", "--format=%H%x09%cI", sha + "^{commit}")
    if code != 0 or "\t" not in out:
        return None, None
    full, stamp = out.strip().split("\t", 1)
    return full, parse_ts(stamp)


def last_rev_before(root, stamp, rel):
    code, out = git(root, "rev-list", "-1", "--before=" + iso(stamp), "HEAD", "--", rel)
    return out.strip() if code == 0 and out.strip() else None


def commits_between(root, since, until, rel=None):
    args = ["log", "--no-merges", "--format=%H%x09%cI", "--since=" + iso(since), "--until=" + iso(until)]
    if rel:
        args += ["--", rel]
    code, out = git(root, *args)
    if code != 0:
        raise Unknown("git log failed in %s" % root)
    rows = []
    for line in out.splitlines():
        sha, stamp = line.split("\t", 1)
        when = parse_ts(stamp)
        if since <= when <= until:
            rows.append((sha, when))
    return list(reversed(rows))


# --- anchor -----------------------------------------------------------------------------


def sections(text):
    """Map lower-cased top-level heading -> its lines."""
    out, current = {}, None
    for line in (text or "").splitlines():
        if line.startswith("# "):
            current = line[2:].strip().lower()
            out.setdefault(current, [])
        elif current is not None:
            out[current].append(line)
    return out


def boxes(text):
    """Closure boxes, `SC<n>` / `DoD<n>` by ordinal in their section."""
    found = {}
    parts = sections(text)
    for heading, prefix in (("success criteria", "SC"), ("definition of done", "DoD")):
        n = 0
        for line in parts.get(heading, []):
            match = CHECKBOX.match(line)
            if match:
                n += 1
                found["%s%d" % (prefix, n)] = {
                    "checked": match.group(1) in "xX",
                    "text": match.group(2).strip(),
                }
    return found


def progress_lines(text):
    return sections(text).get("progress", [])


def added_entries(before, after):
    """New `# Progress` lines, grouped into contiguous entries."""
    pool = {}
    for line in before:
        pool[line] = pool.get(line, 0) + 1
    entries, current = [], []
    for line in after:
        if pool.get(line):
            pool[line] -= 1
            if current:
                entries.append(current)
                current = []
        elif line.strip():
            current.append(line)
        elif current:
            current.append(line)
    if current:
        entries.append(current)
    return ["\n".join(e).strip() for e in entries if "\n".join(e).strip()]


def frontmatter_goals(text):
    lines = (text or "").splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    goals = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        goals += re.findall(r"\[\[([^\]|#]+)", line) if "[[" in line else []
    return [g.strip() for g in goals]


def goal_links(vault, task_name, text):
    """`Goal <box>` for every goal whose `# Tasks` line names this task `— closes <box>`."""
    found = []
    for goal in frontmatter_goals(text):
        for path in glob.glob(os.path.join(vault, "*", glob.escape(goal) + ".md")):
            try:
                with open(path, encoding="utf-8") as handle:
                    body = handle.read()
            except OSError:
                continue
            for line in body.splitlines():
                if "[[%s]]" % task_name in line:
                    found += re.findall(r"closes\s+((?:SC|G)\d+)", line)
    return sorted(set(found))


# --- coverage ---------------------------------------------------------------------------


def load_coverage(path, anchor_boxes):
    """Box id -> `measured` | `asserted` | `none`, copied from the coverage agent's rows."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
    except OSError as exc:
        raise Unknown("coverage unreadable: %s (%s)" % (path, exc.strerror))
    ratings = {}
    for line in raw.splitlines():
        # A box's own text can carry a pipe (`ls … | head -1`), so the columns are
        # read from the right: the last cell is the proposal, the one before it the
        # evidence, and the box is everything before the first separator.
        cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
        if len(cells) < 4:
            continue
        box = re.sub(r"^-?\s*\[[ xX/]\]\s*", "", cells[0]).strip()
        evidence = cells[-2].lower()
        rating = next((r for r in ("measured", "asserted", "none") if evidence.startswith(r)), None)
        if rating is None:
            continue
        ident = None
        token = re.match(r"^((?:SC|DoD)\d+)\b", box)
        if token and token.group(1) in anchor_boxes:
            ident = token.group(1)
        else:
            norm = " ".join(box.lower().split())[:50]
            for key, value in anchor_boxes.items():
                if len(norm) >= 15 and " ".join(value["text"].lower().split()).startswith(norm):
                    ident = key
                    break
        if ident:
            ratings[ident] = rating
    return ratings, hashlib.sha256(raw.encode()).hexdigest()


# --- grading ----------------------------------------------------------------------------


def resolves(token, repos):
    if SHA_TOKEN.fullmatch(token):
        return any(commit_time(r, token)[0] for r in repos)
    return False


def path_resolves(token, bases):
    candidate = os.path.expanduser(token)
    if os.path.isabs(candidate):
        return os.path.exists(candidate)
    return any(os.path.exists(os.path.join(b, candidate)) for b in bases)


def grade_progress(entry, repos, bases):
    if any(line.strip().startswith("```") for line in entry.splitlines()):
        return "B"
    if any(resolves(t, repos) for t in SHA_TOKEN.findall(entry)):
        return "B"
    if any(path_resolves(t, bases) for t in PATH_TOKEN.findall(entry)):
        return "B"
    return "C"


def anchor_observations(before_text, after_text, ref, when, ratings, repos, bases):
    before, after = boxes(before_text), boxes(after_text)
    found = []
    for ident, box in after.items():
        if box["checked"] and not before.get(ident, {}).get("checked"):
            rating = ratings.get(ident) if ratings is not None else None
            if rating == "measured":
                grade, note = "A", "coverage measured"
            elif rating in ("asserted", "none"):
                grade, note = "C", "coverage %s" % rating
            else:
                grade, note = "B", "rating unavailable"
            found.append({"grade": grade, "kind": "tick", "ref": ref, "ts": iso(when),
                          "boxes": [ident], "text": "%s ticked — %s" % (ident, note)})
    for entry in added_entries(progress_lines(before_text), progress_lines(after_text)):
        found.append({"grade": grade_progress(entry, repos, bases), "kind": "progress-entry",
                      "ref": ref, "ts": iso(when), "boxes": sorted(set(BOX_TOKEN.findall(entry))),
                      "text": entry})
    if not found and before_text != after_text:
        found.append({"grade": "C", "kind": "task-edit", "ref": ref, "ts": iso(when), "boxes": [],
                      "text": "task file edited, no box flipped"})
    return found


def mine(root, rel, written):
    return os.path.realpath(os.path.join(root, rel)) in written


def code_observations(root, since, until, written):
    """Commits in a written root that touch a file this session wrote -- a shared root
    (`~/.claude`, a main checkout) carries sibling sessions' commits too."""
    found = []
    for sha, when in commits_between(root, since, until):
        _, names = git(root, "show", "--format=", "--name-only", sha)
        touched = [n for n in names.splitlines() if n]
        if touched and not any(mine(root, n, written) for n in touched):
            continue
        _, diff = git(root, "show", "-w", "--format=", "--unified=0", sha)
        changed = [l for l in diff.splitlines()
                   if l[:1] in "+-" and not l.startswith(("+++", "---"))]
        _, stat = git(root, "show", "-w", "--format=", "--numstat", sha)
        plus = minus = 0
        for row in stat.splitlines():
            cells = row.split("\t")
            if len(cells) >= 2 and cells[0].isdigit() and cells[1].isdigit():
                plus, minus = plus + int(cells[0]), minus + int(cells[1])
        _, message = git(root, "show", "-s", "--format=%B", sha)
        found.append({"grade": "B" if changed else "C", "kind": "commit", "ref": sha, "ts": iso(when),
                      "repo": root, "boxes": sorted(set(BOX_TOKEN.findall(message))),
                      "text": "%s %s +%d −%d" % (os.path.basename(root), sha[:7], plus, minus)
                      if changed else "%s %s empty or whitespace-only" % (os.path.basename(root), sha[:7])})
    return found


def worktree_observation(root, written, last_write):
    _, stat = git(root, "diff", "-w", "--numstat", "HEAD")
    plus = minus = files = 0
    for row in stat.splitlines():
        cells = row.split("\t")
        if (len(cells) >= 3 and cells[0].isdigit() and cells[1].isdigit()
                and int(cells[0]) + int(cells[1]) and mine(root, cells[2], written)):
            plus, minus, files = plus + int(cells[0]), minus + int(cells[1]), files + 1
    _, untracked = git(root, "ls-files", "--others", "--exclude-standard")
    for rel in untracked.splitlines():
        if mine(root, rel, written):
            try:
                with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as handle:
                    plus += sum(1 for _ in handle)
                files += 1
            except OSError:
                pass
    if not files:
        return None
    return {"grade": "B", "kind": "diff", "ref": "worktree", "ts": iso(last_write), "repo": root,
            "boxes": [], "text": "%s uncommitted +%d −%d across %d file(s) (persists to window end)"
            % (os.path.basename(root), plus, minus, files)}


# --- claim ------------------------------------------------------------------------------


def match_ref(obs, ref, kind, box):
    """The observation a claim's `ref` names -- the claimed box and kind first, then any.

    Every uncommitted change shares the ref `worktree`, so a ref alone cannot pick one
    tick out of several; the claimed box is what does."""
    low = ref.strip().lower()

    def hits(o):
        if o["kind"] == "progress-entry" and kind == "progress-entry":
            return bool(low) and low in o["text"].lower()
        if low == "worktree":
            return o["ref"] == "worktree"
        return len(low) >= 7 and o["ref"] != "worktree" and o["ref"].startswith(low)

    ranked = sorted((o for o in obs if hits(o)),
                    key=lambda o: (box not in o["boxes"], o["kind"] != kind))
    return (ranked or [None])[0]


def evaluate_claim(raw, obs, start_boxes, known, ratings, roots, vault, since, start_progress):
    if raw is None:
        return {"input": None, "result": "none filed", "grade": None}
    try:
        claim = json.loads(raw)
    except ValueError:
        return {"input": raw, "result": "rejected:narrative", "grade": None}
    if not isinstance(claim, dict):
        return {"input": raw, "result": "rejected:narrative", "grade": None}
    box, kind, ref = claim.get("box"), claim.get("kind"), claim.get("ref")

    def out(result, grade=None):
        return {"input": claim, "result": result, "grade": grade}

    if kind in ASSERTED_KINDS:
        return out("rejected:asserted")
    if kind not in CLAIM_KINDS or not isinstance(box, str) or not isinstance(ref, str) or not ref.strip():
        return out("rejected:narrative")
    if box not in known:
        return out("rejected:narrative")
    scope = [vault] + list(roots)
    if claim.get("repo"):
        repo = toplevel(str(claim["repo"]))
        if repo not in scope:
            return out("rejected:out-of-scope")
        scope = [repo]
    met = start_boxes.get(box, {}).get("checked", False)
    hit = match_ref([o for o in obs if not claim.get("repo") or o.get("repo", vault) in scope], ref, kind, box)
    if hit is None:
        if kind == "progress-entry" and ref.strip().lower() in "\n".join(start_progress).lower():
            return out("rejected:predates-window")
        for repo in scope:
            full, when = commit_time(repo, ref.strip())
            if full and when < since:
                return out("rejected:predates-window")
        return out("rejected:already-met" if met else "rejected:narrative")
    if met:
        return out("rejected:already-met")
    if kind == "tick" and (ratings or {}).get(box) in ("asserted", "none"):
        return out("rejected:asserted")
    if hit["grade"] in ("A", "B") and box in hit["boxes"]:
        return out("backed", hit["grade"])
    return out("rejected:asserted")


# --- the window -------------------------------------------------------------------------


def default_since(state, session, until):
    path = os.path.join(state, "drive-%s.json" % session)
    try:
        with open(path, encoding="utf-8") as handle:
            last = parse_ts(json.load(handle).get("last_run"))
        if last is not None and last < until:
            return last, "since last drive"
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return until - DEFAULT_WINDOW, "default 15 min"


def measure(args):
    now = parse_ts(args.now) if args.now else datetime.datetime.now(datetime.timezone.utc)
    until = parse_ts(args.until) if args.until else now
    if args.since:
        since, since_source = parse_ts(args.since), "--since"
    else:
        since, since_source = default_since(env_dir("SUPERVISOR_STATE_DIR", "state"), args.session, until)
    capped = since < until - LOOKBACK_CAP
    if capped:
        since = until - LOOKBACK_CAP
    if since >= until:
        raise Unknown("window empty: since %s is not before until %s" % (iso(since), iso(until)))
    report = {"session": args.session, "anchor": os.path.abspath(args.anchor),
              "since": iso(since), "until": iso(until), "now": iso(now),
              "since_source": since_source, "capped": capped}

    transcripts = find_transcripts(env_dir("SUPERVISOR_PROJECTS_DIR", "projects"), args.session)
    registry = find_registry(env_dir("SUPERVISOR_SESSIONS_DIR", "sessions"), args.session)
    if not transcripts and registry is None:
        raise Unknown("session %s is known to neither the registry nor any transcript" % args.session)
    calls, first = load_calls(transcripts)

    anchor = os.path.realpath(args.anchor)
    try:
        with open(anchor, encoding="utf-8") as handle:
            working = handle.read()
        mtime = datetime.datetime.fromtimestamp(os.stat(anchor).st_mtime, datetime.timezone.utc)
    except OSError as exc:
        raise Unknown("anchor unreadable: %s (%s)" % (args.anchor, exc.strerror))
    vault = toplevel(anchor)
    if vault is None:
        raise Unknown("anchor is not inside a git repository: %s" % args.anchor)
    rel = os.path.relpath(anchor, vault)
    fresh = abs(now - until) <= FRESH

    upto = [c for c in calls.values() if c["ts"] <= until]
    window = [c for c in upto if c["ts"] >= since and not is_self(c)]
    written = set()
    for call in upto:
        if call["name"] in WRITE_TOOLS:
            written.update(os.path.realpath(os.path.expanduser(p)) for p in written_paths(call))
    roots = sorted({r for r in (toplevel(p) for p in written) if r and r != vault})

    start_rev = last_rev_before(vault, since, rel)
    start_text = show(vault, start_rev, rel) if start_rev else None
    end_rev = last_rev_before(vault, until, rel)
    end_text = working if fresh else (show(vault, end_rev, rel) if end_rev else None)
    start_boxes, end_boxes = boxes(start_text), boxes(end_text)

    ratings, coverage = None, None
    if args.coverage:
        ratings, digest = load_coverage(args.coverage, end_boxes or start_boxes)
        coverage = {"path": os.path.abspath(args.coverage), "sha256": digest, "rows": len(ratings)}

    repos, bases = [vault] + roots, [vault] + roots
    obs = []
    for sha, when in commits_between(vault, since, until, rel):
        before = show(vault, sha + "^", rel)
        obs += anchor_observations(before, show(vault, sha, rel), sha, when, ratings, repos, bases)
    head_text = show(vault, "HEAD", rel)
    if fresh and working != head_text and since <= mtime <= until:
        obs += anchor_observations(head_text, working, "worktree", mtime, ratings, repos, bases)
    for root in roots:
        obs += code_observations(root, since, until, written)
        if fresh:
            mine = [c["ts"] for c in window if c["name"] in WRITE_TOOLS
                    and any(toplevel(p) == root for p in written_paths(c))]
            if mine:
                diff = worktree_observation(root, written, max(mine))
                if diff:
                    obs.append(diff)
    obs.sort(key=lambda o: o["ts"])
    moved = [o for o in obs if o["grade"] in ("A", "B")]

    # Activity: repeats are the same normalized call with the same result.
    groups = {}
    for call in window:
        if "result" in call:
            groups.setdefault((label(call), call["result"]), []).append(call)
    top = max(groups.items(), key=lambda kv: len(kv[1]), default=None)
    wrote = [c for c in window if is_write(c)]
    activity = {"tool_calls": len(window), "wrote": len(wrote), "reads": len(window) - len(wrote),
                "top_repeat": {"count": len(top[1]), "label": top[0][0][:120]} if top and len(top[1]) > 1 else None}

    # The trailing call in flight, bounded per tool name.
    pending = None
    open_calls = [c for c in upto if ("result_ts" not in c or c["result_ts"] > until) and not is_self(c)]
    if open_calls:
        last = max(open_calls, key=lambda c: c["ts"])
        done = [(c["result_ts"] - c["ts"]).total_seconds() for c in upto
                if c["name"] == last["name"] and "result_ts" in c and c["result_ts"] <= until]
        bound = max(RUNNING_FLOOR, 2 * statistics.median(done)) if done else RUNNING_NO_HISTORY
        pending = {"name": last["name"], "since": iso(last["ts"]),
                   "age_s": int((until - last["ts"]).total_seconds()), "bound_s": int(bound)}

    started = parse_ts(registry.get("startedAt")) if registry and registry.get("startedAt") else first
    open_boxes = [k for k, v in end_boxes.items() if not v["checked"]]

    subtype = None
    if pending and pending["age_s"] < pending["bound_s"]:
        verdict = "TOOL-RUNNING"
    elif end_boxes and not open_boxes:
        verdict = "DONE-CANDIDATE"
    elif moved:
        verdict = "PROGRESSING"
    elif started is not None and started > until - ORIENTATION:
        verdict = "ORIENTING"
    elif not window:
        verdict = "IDLE"
    else:
        verdict = "BUSY-NO-MOVE"
        subtype = classify_busy(groups, window, wrote)

    claim = evaluate_claim(args.claim, obs, start_boxes, set(start_boxes) | set(end_boxes), ratings,
                           roots, vault, since,
                           progress_lines(start_text))
    task_name = os.path.splitext(os.path.basename(anchor))[0]
    goal_boxes = goal_links(vault, task_name, end_text or working)
    toward = []
    for o in moved:
        for box in o["boxes"] or [None]:
            chain = "untagged" if box is None else " → ".join(
                ["Task %s" % box] + ["Goal %s" % g for g in goal_boxes])
            if chain not in toward:
                toward.append(chain)

    report.update({
        "verdict": verdict, "subtype": subtype,
        "short_path": verdict == "PROGRESSING" and claim["result"] == "backed" and claim["grade"] == "A",
        "sources": {"transcripts": transcripts,
                    "registry": {"status": registry.get("status")} if registry else None,
                    "vault_root": vault, "written_roots": roots, "worktree_read": fresh,
                    "coverage": coverage},
        "boxes": {"total": len(end_boxes), "open": open_boxes},
        "observations": obs, "moved": moved, "toward": toward,
        "activity": activity, "pending": pending,
        "session_started": iso(started) if started else None,
        "claim": claim,
    })
    report["block"] = render(report)
    return report


def classify_busy(groups, window, wrote):
    if any(len(v) >= LOOP_MIN for v in groups.values()):
        return "LOOP"
    edits = [c for c in window if c["name"] == "Edit"]
    for i, later in enumerate(edits):
        for earlier in edits[:i]:
            a, b = earlier["input"], later["input"]
            if (a.get("file_path") == b.get("file_path") and a.get("old_string") == b.get("new_string")
                    and a.get("new_string") == b.get("old_string")):
                return "OSCILLATE"
    if not wrote and len(window) >= READ_LOOP_MIN:
        return "READ-LOOP"
    return None


def local_hm(stamp):
    return parse_ts(stamp).astimezone().strftime("%H:%M")


def render(report):
    head = "⏱ Window %s–%s (%s, measured): " % (local_hm(report["since"]), local_hm(report["until"]),
                                              report["since_source"])
    verdict = report["verdict"] + ("/" + report["subtype"] if report.get("subtype") else "")
    lines = [head + verdict]
    moved = report["moved"] or [None]
    for i, o in enumerate(moved):
        tag = "  Moved:     " if i == 0 else "             "
        if o is None:
            lines.append(tag + "none")
        else:
            ref = "" if o["kind"] in ("commit", "diff") else (
                " (uncommitted)" if o["ref"] == "worktree" else " (commit %s)" % o["ref"][:7])
            text = o["text"].splitlines()[0][:100]
            lines.append(tag + "%s  %s%s" % (o["grade"], text, ref))
    lines.append("  Toward:    " + (" · ".join(report["toward"]) or "none"))
    act = report["activity"]
    activity = "%d tool calls · %d wrote" % (act["tool_calls"], act["wrote"])
    if act["top_repeat"]:
        activity += " · %d identical repeats (`%s`)" % (act["top_repeat"]["count"], act["top_repeat"]["label"][:60])
    lines.append("  Activity:  " + activity)
    claim = report["claim"]
    shown = "none filed" if claim["input"] is None else "%s — %s" % (
        json.dumps(claim["input"], ensure_ascii=False) if not isinstance(claim["input"], str) else claim["input"],
        claim["result"])
    lines.append("  Claim:     " + shown)
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--session", required=True)
    parser.add_argument("--anchor", required=True)
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--coverage")
    parser.add_argument("--claim")
    parser.add_argument("--now", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = measure(args)
        code = OK
    except (Unknown, ValueError) as exc:
        report = {"session": args.session, "anchor": args.anchor, "since": args.since, "until": args.until,
                  "verdict": "UNKNOWN", "reason": str(exc), "short_path": False}
        print("UNKNOWN: %s" % exc, file=sys.stderr)
        code = UNKNOWN_EXIT
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    elif report["verdict"] == "UNKNOWN":
        print("⏱ Window: UNKNOWN — " + report["reason"])
    else:
        print(render(report))
    return code


if __name__ == "__main__":
    sys.exit(main())
