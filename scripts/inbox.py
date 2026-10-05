#!/usr/bin/env python3
"""inbox — the operator's approval queue, as one read-only view.

`phase: todo` IS the operator's approval boundary: agents file there and stop,
and the operator's `todo -> planning` flip is the approval. This script renders
that boundary as a readable list, grouped so a triage pass can be done in one
sitting rather than by walking the vault.

READ-ONLY, and it must stay that way. This script writes no task file, no cache
and no state file. The three verbs that move a row (approve / reject / later)
belong to `commands/inbox.md`, and they run through `vault-cli` — the only
writer of an approval. A render that mutated anything would make the view
indistinguishable from the act, which is the one property the whole surface
depends on.

WHAT COUNTS AS LIVE. A row is in the inbox when `phase` is `todo` AND its
`status` is neither terminal (`completed`, `aborted`) nor a deliberate park
(`backlog`, `hold`). `backlog` is excluded for a specific reason rather than for
tidiness: `later` SENDS a row there, so a row already at `backlog` is one the
operator has already answered — listing it again would re-ask a settled
question on every render.

⚠️ Excluding `backlog` is not the same as excluding a stale row; the two are
independent and this script does not judge staleness. It reports what sits at
the boundary. The audit that RANKS rows is `agents/manager-drive.md` clause
(1)'s, not this one's.

WHAT IS EXCLUDED AS RECURRING. `recurring:` frontmatter, or a cadence-marked
title carrying an ISO-week token (`2026W35`, optionally with a weekday suffix).
⚠️ A plain ISO DATE in a title is NOT a cadence marker and must not be treated
as one — `Fix Failed CI Build claude-supervisor 2026-09-30` is a one-off whose
title happens to be dated, and a date-shaped test would silently drop every such
row from the inbox. The week token is the discriminator; keep it that way.

TITLES ARE NOT NAMESPACED. One vault's `a recurring task - 2026-09-21` has
coexisted with another vault's task of the same name, as two different tasks.
Every row therefore prints its vault even when it is grouped under a topic:
grouping is a reading aid, the vault is the identity.

A VAULT THAT CANNOT ANSWER IS SKIPPED AND NAMED, NEVER FATAL. One configured
vault returns literal `null` rather than a task list, and empty stdin does the
same; either would otherwise abort the scan mid-loop and silently truncate every
vault after it in iteration order. The failure mode to avoid is a SHORT list
that reads as a complete one.

RESOLVING A ROW TO ITS VAULT. `--resolve <title>` prints the vault owning a live
row, because every verb must pass `--vault` (ten vaults declare a `tasks_dir`,
and the configured default is one of them). It REFUSES on ambiguity rather than
picking: titles are not namespaced, so the same title can be live in two vaults
at once and a verb that guessed would move the wrong row in the wrong vault.
Resolution reads the same live set the view renders, so a row already approved,
rejected or deferred resolves to nothing — which is the honest answer, and it is
what stops a second verb from re-deciding a settled row.

SCOPE — WHO SEES WHICH ROWS. A session with a manager record at
`~/.claude/state/worker-manager/<session-id>.json` sees only its subject's rows
(a row's topic, or one of its `goals:`, equals the subject). Every other session
sees the RESIDUAL: rows no LIVE manager handles. Live is decided by
`manager-liveness.py`'s own `classify()` over `sweep-gate/<slug>.cadence` — never
by the record files, which outlive their managers, and never through `--check`,
which writes its notified-set file and would break this script's read-only rule.
This mirrors the open-items rule: a subject's manager carries its asks, the fleet
manager carries the residual.

RANKED, NEVER THE TAIL. The view renders at most `agents/manager-drive.md`
clause (7)'s cap, in that clause's order: a row serving a goal, then a row other
live rows are `blocked_by`, then the rest by cached `task-auditor` score
(`manager-predispatch/*.verdicts.json`; rows with no entry sort after, by name).
The cap is READ from clause (7) — never restated here. Ordering picks nothing:
no row is marked as the one to approve, and every verb still needs a named row.
`--all` restores the flat, every-row render.

TWO RESOLVERS, AND THEY ANSWER DIFFERENT QUESTIONS. `--resolve` asks the inbox
question — "is this row still decision-pending?" — and is scoped to `phase: todo`
by the same filter the view uses, so it returns rc 3 for an already-approved row
by design. `--locate` asks the *lookup* question — "which vault holds this exact
title?" — and scans every task file at any phase. A caller that already holds a
name and needs its vault wants `--locate`; using `--resolve` there resolves the
complement of what it holds. Measured 2026-10-05: `/supervisor:ready` operates on
approved rows, so `--resolve` refused every row it exists to ready. `--locate`
lives here because this script already owns vault enumeration and frontmatter
parsing; it is not a second inbox reader.

Usage:
    inbox.py [--vault <name>] [--json] [--all] [--session <id>]
    inbox.py --resolve <title>
    inbox.py --locate <title>

Exit codes: 0 ok, 2 vault config unreadable, 4 ambiguous, and 3 for both
resolvers — `--resolve` reads it as "no such *live* row", `--locate` as "no such
row at any phase". The per-verb stderr strings say which; this line covers both.
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

# A cadence-marked title carries an ISO-week token; a dated one-off does not.
# See the module docstring for why the date form is deliberately absent.
CADENCE_RE = re.compile(r"\d{4}W\d{2}")

# Terminal and parked statuses. `backlog` is here because `later` writes it.
NOT_LIVE = {"completed", "aborted", "backlog", "hold"}

FM_RE = re.compile(r"\A---\n(.*?)\n---", re.S)

# `Topic: [[Manager Layer]]` — the topic-direct convention. A row may also carry
# a `topics:` frontmatter list; both are read, frontmatter first.
TOPIC_LINE_RE = re.compile(r"^Topic:\s*\[\[([^\]|]+)", re.M)

WHY_WIDTH = 96

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
MANAGER_DIR = os.path.expanduser(os.environ.get(
    "SUPERVISOR_WORKER_MANAGER_DIR", "~/.claude/state/worker-manager"))
VERDICTS_DIR = os.path.expanduser(os.environ.get(
    "SUPERVISOR_PREDISPATCH_DIR", "~/.claude/state/manager-predispatch"))
CLAUSE7 = os.path.join(SCRIPTS, os.pardir, "agents", "manager-drive.md")
CAP_RE = re.compile(r"recommend \*\*at most (\d+)\*\*")
WIKI_RE = re.compile(r"\[\[([^\]|]+)")


def load_vaults():
    """[(name, path, tasks_dir, has_topics_dir)] from vault-cli's own config.

    vault-cli is the single source of truth for where a vault keeps its tasks,
    so a folder rename (`24 Tasks` -> `25 Tasks`) needs no edit here. Raises
    rather than returning [] so the caller can say "nothing readable" instead of
    rendering an empty inbox that reads like a clean one.
    """
    res = subprocess.run(
        ["vault-cli", "--output", "json", "config", "list"],
        capture_output=True, text=True, timeout=20,
    )
    if res.returncode != 0:
        raise RuntimeError(f"vault-cli config list failed: {res.stderr.strip()}")
    out = []
    for v in json.loads(res.stdout) or []:
        tasks_dir = v.get("tasks_dir")
        path = os.path.expanduser(v.get("path") or "")
        if tasks_dir and path:
            out.append((v.get("name") or os.path.basename(path), path,
                        tasks_dir, bool(v.get("topics_dir"))))
    return sorted(out)


def parse(text):
    """(frontmatter dict, body). Frontmatter values are kept as raw strings."""
    m = FM_RE.match(text)
    if not m:
        return {}, text
    fm, key = {}, None
    for line in m.group(1).splitlines():
        km = re.match(r"^([a-z_]+):\s*(.*)$", line)
        if km:
            key = km.group(1)
            fm[key] = km.group(2).strip().strip("'\"")
            continue
        # A YAML block list (`topics:` then `  - '[[X]]'`) — folded into the same
        # comma-joined string an inline list yields, so readers see one shape.
        li = re.match(r"^\s+-\s*(.*)$", line)
        if key and li:
            item = li.group(1).strip().strip("'\"")
            fm[key] = f"{fm[key]}, {item}" if fm[key] else item
    return fm, text[m.end():]


def names(raw):
    """Wikilink targets (or bare items) from a raw frontmatter list value."""
    if not raw or raw == "[]":
        return []
    hits = WIKI_RE.findall(raw)
    if hits:
        return [h.strip() for h in hits]
    return [x.strip().strip("'\"") for x in raw.strip("[]").split(",") if x.strip()]


def is_recurring(fm, title):
    return "recurring" in fm or bool(CADENCE_RE.search(title))


def why_it_matters(body):
    """The `# Impact` first paragraph, else the first paragraph. One line."""
    m = re.search(r"^# Impact\s*\n(.*?)(?=\n# |\Z)", body, re.S | re.M)
    para = m.group(1) if m else body
    for chunk in re.split(r"\n\s*\n", para):
        line = " ".join(chunk.split())
        if line:
            return line[:WHY_WIDTH] + ("…" if len(line) > WHY_WIDTH else "")
    return ""


def topic_of(fm, body, vault_has_topics):
    """The row's group. A topic when the vault has one, else None (-> vault)."""
    if not vault_has_topics:
        return None
    topics = names(fm.get("topics"))
    if topics:
        return topics[0]
    m = TOPIC_LINE_RE.search(body)
    return m.group(1).strip() if m else None


def scan(vaults, only=None, *, inbox_only=True):
    """([(group, row)], [skipped]) for every row the caller asked for.

    `inbox_only` (the default) keeps only `phase: todo` rows — the inbox
    population the view and `--resolve` both serve. `--locate` passes False:
    a caller resolving a name it already holds is not asking the inbox
    question, and answering it there is what made `/supervisor:ready` refuse
    every row it exists to ready. Ready's input is the *complement* of the
    inbox — Gate 1 refuses `phase: todo` and proceeds only on approved rows —
    so a resolver scoped to `phase: todo` resolves precisely the set ready
    must reject. Measured 2026-10-05.
    """
    rows, skipped = [], []
    for name, path, tasks_dir, has_topics in vaults:
        if only and name != only:
            continue
        d = os.path.join(path, tasks_dir)
        if not os.path.isdir(d):
            skipped.append(f"{name} (no {tasks_dir}/)")
            continue
        try:
            entries = sorted(os.listdir(d))
        except OSError as e:
            skipped.append(f"{name} ({e.strerror})")
            continue
        for entry in entries:
            if not entry.endswith(".md"):
                continue
            try:
                with open(os.path.join(d, entry), encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError) as e:
                skipped.append(f"{name}/{entry} ({e.__class__.__name__})")
                continue
            fm, body = parse(text)
            if inbox_only and (fm.get("phase") != "todo" or fm.get("status") in NOT_LIVE):
                continue
            title = entry[:-3]
            if inbox_only and is_recurring(fm, title):
                continue
            group = topic_of(fm, body, has_topics) or name
            rows.append((group, {"title": title, "vault": name,
                                 "why": why_it_matters(body),
                                 "goals": names(fm.get("goals")),
                                 "blocked_by": names(fm.get("blocked_by"))}))
    return rows, skipped


def render(rows, skipped, out=sys.stdout):
    if not rows:
        print("✅ Inbox empty — no live row sits at `phase: todo`.", file=out)
    groups = {}
    for group, row in rows:
        groups.setdefault(group, []).append(row)
    print(f"⌛ {len(rows)} unapproved · {len(groups)} group(s) · "
          f"read-only, nothing written", file=out)
    for group in sorted(groups, key=lambda g: (-len(groups[g]), g)):
        members = sorted(groups[group], key=lambda r: r["title"])
        print(f"\n▸ {group} ({len(members)})", file=out)
        for r in members:
            print(f"  • {r['title']}", file=out)
            print(f"    [{r['vault']}] {r['why']}", file=out)
    if skipped:
        print(f"\n⚠️ skipped {len(skipped)}: " + "; ".join(skipped), file=out)


def slug(subject):
    """Same slug as manager-liveness.py / sweep-gate.py."""
    return re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")


def subjects_of(group, row):
    return {slug(group)} | {slug(g) for g in row.get("goals", [])}


def session_subject(session_id):
    """The manager record's subject for this session, or None (-> residual)."""
    if not session_id:
        return None
    try:
        with open(os.path.join(MANAGER_DIR, session_id + ".json"),
                  encoding="utf-8") as fh:
            return json.load(fh).get("subject") or None
    except (OSError, ValueError):
        return None


def live_slugs(now=None):
    """Slugs whose manager loop is live, per manager-liveness.py's classify().

    Imported, not shelled out: `--check` writes liveness-notified.json, and this
    script writes nothing.
    """
    sp = importlib.util.spec_from_file_location(
        "manager_liveness", os.path.join(SCRIPTS, "manager-liveness.py"))
    ml = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(ml)
    now = time.time() if now is None else now
    try:
        cadences = [f[:-len(".cadence")] for f in os.listdir(ml.STATE_DIR)
                    if f.endswith(".cadence")]
    except FileNotFoundError:
        return set()
    return {s for s in cadences if ml.classify(s, now)[0] == "OK"}


def scope(rows, subject, live):
    """Rows for `subject`, or — subject None — the residual no live manager owns."""
    if subject:
        want = slug(subject)
        return [(g, r) for g, r in rows if want in subjects_of(g, r)]
    return [(g, r) for g, r in rows if not (subjects_of(g, r) & live)]


def clause7_cap(path=CLAUSE7):
    with open(path, encoding="utf-8") as fh:
        m = CAP_RE.search(fh.read())
    if not m:
        raise RuntimeError(f"no cap found in {path} clause (7)")
    return int(m.group(1))


def cached_scores():
    """{title: score} from every verdicts cache; read-only."""
    scores = {}
    try:
        files = [f for f in os.listdir(VERDICTS_DIR) if f.endswith(".verdicts.json")]
    except FileNotFoundError:
        return scores
    for f in sorted(files):
        try:
            with open(os.path.join(VERDICTS_DIR, f), encoding="utf-8") as fh:
                data = json.load(fh) or {}
        except (OSError, ValueError):
            continue
        for title, v in data.items():
            if isinstance(v, dict) and isinstance(v.get("score"), (int, float)):
                scores[title] = max(scores.get(title, v["score"]), v["score"])
    return scores


def rank(rows, scores):
    """Clause (7)'s order: goal-serving, then unblockers, then score; stable by name."""
    blockers = {b for _, r in rows for b in r.get("blocked_by", [])}

    def key(item):
        _, r = item
        tier = 0 if r.get("goals") else 1 if r["title"] in blockers else 2
        score = scores.get(r["title"])
        return (tier, score is None, -(score or 0), r["title"], r["vault"])
    return sorted(rows, key=key)


def render_ranked(ranked, cap, label, skipped, scores, out=sys.stdout):
    shown = ranked[:cap]
    print(f"⌛ {label} · {len(ranked)} unapproved · showing next {len(shown)} "
          f"(clause (7) order) · read-only, nothing written", file=out)
    if not ranked:
        print("✅ Nothing in this scope sits at `phase: todo`.", file=out)
    for i, (group, r) in enumerate(shown, 1):
        score = scores.get(r["title"])
        print(f"{i}. {r['title']}", file=out)
        print(f"   [{r['vault']}] {group} · "
              f"{f'{score}/10' if score is not None else 'unscored'} · {r['why']}",
              file=out)
    rest = len(ranked) - len(shown)
    if rest > 0:
        print(f"\n{rest} more not shown — `inbox.py --all` renders every row", file=out)
    if skipped:
        print(f"\n⚠️ skipped {len(skipped)}: " + "; ".join(skipped), file=out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault", help="narrow the scan to one vault")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--all", action="store_true",
                    help="every live row, flat and unscoped")
    ap.add_argument("--session", default=os.environ.get("CLAUDE_CODE_SESSION_ID"),
                    help="session whose manager record scopes the view")
    ap.add_argument("--resolve", metavar="TITLE",
                    help="print the vault owning this live row (refuses if ambiguous)")
    ap.add_argument("--locate", metavar="TITLE",
                    help="print the vault holding this task at ANY phase "
                         "(refuses if ambiguous)")
    args = ap.parse_args(argv)

    try:
        vaults = load_vaults()
    except Exception as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2
    if not vaults:
        print("❌ no vault declares a tasks_dir", file=sys.stderr)
        return 2

    if args.locate:
        rows, skipped = scan(vaults, args.vault, inbox_only=False)
        hits = sorted({r["vault"] for _, r in rows if r["title"] == args.locate})
        if not hits:
            print(f"❌ no task named {args.locate!r} in any scanned vault",
                  file=sys.stderr)
            if skipped:
                # A SHORT list must never read as a complete one: without this the
                # rc 3 message asserts absence when a vault simply failed to parse.
                print(f"⚠️ {len(skipped)} vault(s) could not be read — the row may "
                      f"exist there: " + "; ".join(skipped), file=sys.stderr)
            return 3
        if len(hits) > 1:
            print(f"❌ {args.locate!r} exists in more than one vault: "
                  + ", ".join(hits) + " — pass --vault to say which", file=sys.stderr)
            return 4
        print(hits[0])
        return 0

    rows, skipped = scan(vaults, args.vault)
    if args.resolve:
        hits = sorted({r["vault"] for _, r in rows if r["title"] == args.resolve})
        if not hits:
            print(f"❌ no live row named {args.resolve!r} — it is approved, "
                  f"rejected, deferred, or does not exist", file=sys.stderr)
            return 3
        if len(hits) > 1:
            print(f"❌ {args.resolve!r} is live in more than one vault: "
                  + ", ".join(hits) + " — pass --vault to say which", file=sys.stderr)
            return 4
        print(hits[0])
        return 0
    if args.all:
        if args.json:
            json.dump({"count": len(rows), "skipped": skipped,
                       "rows": [{"group": g, **r} for g, r in rows]},
                      sys.stdout, indent=2)
            print()
        else:
            render(rows, skipped)
        return 0
    try:
        cap = clause7_cap()
    except (OSError, RuntimeError) as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2
    subject = session_subject(args.session)
    scoped = scope(rows, subject, set() if subject else live_slugs())
    scores = cached_scores()
    ranked = rank(scoped, scores)
    label = f"subject: {subject}" if subject else "residual (no live manager)"
    if args.json:
        json.dump({"scope": subject or "residual", "count": len(ranked), "cap": cap,
                   "skipped": skipped,
                   "rows": [{"group": g, "score": scores.get(r["title"]), **r}
                            for g, r in ranked[:cap]]},
                  sys.stdout, indent=2)
        print()
    else:
        render_ranked(ranked, cap, label, skipped, scores)
    return 0


if __name__ == "__main__":
    sys.exit(main())
