#!/usr/bin/env python3
"""Replay a recorded set of permission cards through `permission-tier.py` and count the tiers.

Why a replay and not a live round: live sessions change between rounds, so "the same session set"
cannot be re-run. A recorded set of card payloads can. The set and the ledger rows it was joined
against are both files, so the population is fixed and the result is reproducible.

Inputs:
  BASELINE  {"cards": [{"item_id", "state", "session", "payload", …}, …]} — the cards
  LEDGER    [{"session_id", "mode", "launcher", "spawner_session_id", …}, …] — spawn-ledger rows

The population is the cards whose session is `mode: headless`, has a wrapper launcher and has a
spawner: the workers an owning manager can answer for. `--forms REGEX_SCRIPT` names an independent
regex classifier (a module exposing `cls(payload)` returning `operator:<form>` / `not-operator`);
when given, the moved set is checked against it and any hit is printed by item id — never by
payload, which may carry a secret.

Exit 0 always; the numbers are the result. Payloads are never printed.
"""

import argparse
import collections
import importlib.util
import json
import os
import re
import shlex
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# A moved card the independent regex calls operator is a DISAGREEMENT, and each one is either a
# known coarse spot in that regex or a classifier miss. These are the only coarse spots accepted;
# anything else is `unexplained`, and one unexplained card fails the replay. Each rule re-derives its class from the payload —
# a label alone is never enough.
_RM = re.compile(r"(?:^|[\s;&|(])rm\s+((?:-\S+\s+)*)([^;&|\n]+)")


def _argvs(tier, payload):
    """Every simple command's argv in a Bash payload, nested substitutions included; None if unlexable."""
    if not payload.startswith("Bash:"):
        return None
    out, todo = [], [payload.split(":", 1)[1].lstrip()]
    while todo:
        segs, nested, _ = tier.lex(todo.pop())
        todo.extend(nested)
        for s in segs:
            out.append(tier.words(s)[1])
    return out


def explain_by_position(tier, form, payload):
    """`text-only-mention` when the regex's token never sits where the shell would act on it."""
    try:
        argvs = _argvs(tier, payload)
    except (tier.Unverifiable, ValueError):
        return None
    if argvs is None:
        tool, inp = tier.parse_payload(payload)
        if tool in ("Edit", "Write") and not tier.classify_tool(tool, inp):
            return "path-names-not-a-secret-store" if form == "secret" else "path-only-mention"
        return None
    heads = {os.path.basename(a[0]) for a in argvs if a}
    if form == "delete" and "rm" not in heads and not any("-delete" in a for a in argvs):
        return "text-only-mention"
    if form == "secret" and not heads & {"teamvault", "teamvault-cli", "security", "op", "pass"} \
            and not any(tier._SECRET_PATH.search(x) for a in argvs for x in a):
        return "text-only-mention"
    if form == "prod" and not any(tier._prod({}, a) or tier._deploy({}, a) for a in argvs):
        return "text-only-mention"
    return None


def explain(form, payload, tier=None):
    """The coarse-spot class a regex hit falls in, or None when it is unexplained."""
    if tier is not None:
        why = explain_by_position(tier, form, payload)
        if why:
            return why
    if form == "delete":
        hits = list(_RM.finditer(payload))
        if hits and all(all(t.startswith(("/tmp/", "/private/tmp/")) for t in shlex.split(m.group(2))
                            if not t.startswith("-") and not t.startswith(("2>", ">")))
                        for m in hits):
            return "rm-confined-to-tmp"
        return None
    if form == "secret":
        tokens = re.findall(r"\.ssh/|\.netrc|\.aws/|\.config/gh|\.env\b|security find-", payload)
        if tokens:
            return None
        if re.search(r"(^|[\s;&|(])(teamvault|teamvault-cli)\s", payload):
            return None
        return "teamvault-repo-path"
    if form == "publish":
        if re.search(r"gh\s+(-R\s+\S+\s+)?release\s+(view|list)\b", payload) and not re.search(
                r"gh\s+(-R\s+\S+\s+)?release\s+(create|edit|delete|upload)\b|gmail send|chat messages send|"
                r"npm publish|docker push|curl\b.*-X ?(POST|PUT|DELETE|PATCH)", payload):
            return "release-read"
        return None
    if form == "prod":
        if re.search(r"BRANCH=prod|kubectl(nuke|small)?prod|nuke-deploy prod", payload):
            return None
        if all(re.match(r"[.\w-]", payload[m.end():m.end() + 1] or "/") for m in re.finditer(r"[\w/.-]-prod\b", payload)):
            return "prod-inside-identifier"
        return None
    return None


def population(cards, ledger):
    rows = {r.get("session_id"): r for r in ledger}
    out = []
    for c in cards:
        r = rows.get(c.get("session"))
        if r and r.get("mode") == "headless" and r.get("launcher") and r.get("spawner_session_id"):
            out.append(c)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("baseline")
    ap.add_argument("ledger")
    ap.add_argument("--forms", help="independent regex module with cls(payload)")
    ns = ap.parse_args(argv)
    tier = _load(os.path.join(_HERE, "permission-tier.py"), "permission_tier")
    entries = tier.load_allowlist()
    cards = json.load(open(ns.baseline, encoding="utf-8"))["cards"]
    ledger = json.load(open(ns.ledger, encoding="utf-8"))
    pop = population(cards, ledger)
    forms = _load(ns.forms, "independent_forms") if ns.forms else None
    results = {}
    for name, subset in (("all", pop), ("board-answered", [c for c in pop if c.get("state") == "answered"])):
        tiers = collections.Counter()
        op_forms = collections.Counter()
        moved_hits = []
        for c in subset:
            t = tier.tier_for_payload(c["payload"], entries)
            tiers[t["tier"]] += 1
            if t["tier"] == "operator":
                op_forms[t["form"]] += 1
            elif forms is not None:
                verdict = forms.cls(c["payload"])
                if verdict.startswith("operator:") and verdict != "operator:unverifiable":
                    form = verdict.split(":", 1)[1]
                    try:
                        why = explain(form, c["payload"], tier)
                    except ValueError:
                        why = None
                    moved_hits.append((c.get("item_id"), form, why))
        explained = collections.Counter(w for _, _, w in moved_hits if w)
        unexplained = [(i, f) for i, f, w in moved_hits if not w]
        results[name] = {"cards": len(subset), "tiers": dict(tiers), "operator_forms": dict(op_forms),
                         "moved_regex_form_hits": len(moved_hits), "explained": dict(explained),
                         "unexplained": len(unexplained)}
        print(f"{name}: cards={len(subset)} tiers={dict(tiers)}")
        print(f"  operator forms: {dict(op_forms)}")
        if forms is not None:
            print(f"  moved out of operator with a regex-form hit: {len(moved_hits)}"
                  f" — explained {dict(explained)}, UNEXPLAINED {len(unexplained)}")
            for item_id, form in unexplained[:40]:
                print(f"    unexplained {item_id} {form}")
    print(json.dumps(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
