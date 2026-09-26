#!/usr/bin/env python3
"""Call-graph walk from every transport call site to its in-repo callers, to closure.

The second of the two independent enumeration mechanisms in the transport-read audit
(the first is a grep for the transport call shapes). Any read one mechanism reaches
and the other misses is a disagreement the audit must name rather than average away.

Why a walk at all: a transport call site is not a read site. `who-needs-me.py`'s
`panes()` wraps `wezterm_panes()`, and `fleet-board.py` consumes `panes()` — so the
caller that renders a false empty is *two* hops from the subprocess. A one-hop walk
reaches the wrapper and stops; this one iterates to closure.

Usage:
    python3 scripts/transport-read-walk.py            # human-readable edge list
    python3 scripts/transport-read-walk.py --json     # machine-readable
"""
import argparse
import ast
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# A transport read is a subprocess call whose argv[0] names one of these.
TRANSPORT_ARGV0 = ("wezterm", "ps")


def source_files():
    return sorted(
        os.path.join(HERE, n)
        for n in os.listdir(HERE)
        if n.endswith(".py") and os.path.isfile(os.path.join(HERE, n))
    )


def local_name(path):
    return os.path.basename(path)


def parse(path):
    with open(path, encoding="utf-8") as fh:
        return ast.parse(fh.read(), filename=path)


def functions(tree):
    """Every function defined in the module, by name (last definition wins)."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node
    return out


def module_aliases(tree):
    """Local name -> sibling .py basename, from importlib load assignments.

    Handles both shapes in this repo:
        wnm = _load("who_needs_me", "who-needs-me.py")            # fleet-board.py
        mod = importlib.util.module_from_spec(spec)                # jump.py
    """
    aliases = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            for sub in ast.walk(node.value):
                if not isinstance(sub, ast.Call):
                    continue
                for arg in sub.args:
                    if (
                        isinstance(arg, ast.Constant)
                        and isinstance(arg.value, str)
                        and arg.value.endswith(".py")
                    ):
                        aliases[target.id] = arg.value
    return aliases


def subprocess_tools(node):
    """The transport tools this function's own subprocess calls name."""
    found = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        fn = sub.func
        if not (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)):
            continue
        if fn.value.id != "subprocess":
            continue
        for arg in sub.args:
            if isinstance(arg, ast.List) and arg.elts:
                head = arg.elts[0]
                if isinstance(head, ast.Constant) and head.value in TRANSPORT_ARGV0:
                    found.add(head.value)
    return found


def callees(node, aliases):
    """In-repo functions this node calls, as (basename|None, funcname).

    A bare `panes()` is same-module. `wnm.panes()` resolves through the alias map
    to `who-needs-me.py`. Anything else (stdlib, `self.x`) is not in-repo.
    """
    out = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        fn = sub.func
        if isinstance(fn, ast.Name):
            out.add((None, fn.id))
        elif isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
            target = aliases.get(fn.value.id)
            if target:
                out.add((target, fn.attr))
    return out


def build():
    """Returns (functions_by_file, seeds, edges)."""
    per_file = {}
    for path in source_files():
        tree = parse(path)
        per_file[local_name(path)] = {
            "tree": tree,
            "functions": functions(tree),
            "aliases": module_aliases(tree),
        }

    seeds, edges = [], []
    for basename, info in per_file.items():
        for fname, node in info["functions"].items():
            tools = subprocess_tools(node)
            if tools:
                seeds.append(
                    {"file": basename, "function": fname, "tools": sorted(tools)}
                )
            for target, callee in callees(node, info["aliases"]):
                callee_file = target or basename
                if callee_file not in per_file:
                    continue
                if callee not in per_file[callee_file]["functions"]:
                    continue
                edges.append(
                    {
                        "from_file": basename,
                        "from_function": fname,
                        "to_file": callee_file,
                        "to_function": callee,
                    }
                )
    return per_file, seeds, edges


def walk_to_closure(per_file, seeds, edges):
    """BFS from every transport call site through its CALLERS, to closure.

    The direction is the whole point. The audit asks which functions *consume* a
    transport read, so the walk follows edges backwards (callee -> caller). A walk
    that follows callees instead enumerates what the transport function itself
    calls and never reaches the consumers that render the false empty — it misses
    `fleet-board.py`, which calls the wrapper `panes()` and holds no subprocess of
    its own.
    """
    by_callee = {}
    for e in edges:
        by_callee.setdefault((e["to_file"], e["to_function"]), []).append(e)

    seen, queue, reached = set(), [], []
    for s in seeds:
        key = (s["file"], s["function"])
        seen.add(key)
        queue.append((key, 0, None))

    while queue:
        (f, fn), depth, via = queue.pop(0)
        reached.append(
            {
                "file": f,
                "function": fn,
                "depth": depth,
                "reached_via": via,
                "tools": next(
                    (s["tools"] for s in seeds if s["file"] == f and s["function"] == fn),
                    [],
                ),
            }
        )
        for e in by_callee.get((f, fn), []):
            nxt = (e["from_file"], e["from_function"])
            if nxt in seen:
                continue
            seen.add(nxt)
            queue.append((nxt, depth + 1, f"{f}:{fn}"))
    return reached


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args()

    per_file, seeds, edges = build()
    reached = walk_to_closure(per_file, seeds, edges)

    if args.json:
        print(json.dumps({"seeds": seeds, "edges": edges, "reached": reached}, indent=2))
        return 0

    print(f"== transport call sites (seeds): {len(seeds)}")
    for s in sorted(seeds, key=lambda x: (x["file"], x["function"])):
        print(f"  {s['file']}:{s['function']}  [{', '.join(s['tools'])}]")
    print()
    print(f"== caller edges: {len(edges)}")
    for e in sorted(edges, key=lambda x: (x["from_file"], x["from_function"], x["to_file"], x["to_function"])):
        print(f"  {e['from_file']}:{e['from_function']} -> {e['to_file']}:{e['to_function']}")
    print()
    print(f"== closure from the seeds: {len(reached)} functions")
    for r in sorted(reached, key=lambda x: (x["depth"], x["file"], x["function"])):
        via = f"  (via {r['reached_via']})" if r["reached_via"] else ""
        tools = f" [{', '.join(r['tools'])}]" if r["tools"] else ""
        print(f"  d{r['depth']}  {r['file']}:{r['function']}{tools}{via}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
