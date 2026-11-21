#!/usr/bin/env python3
"""`sweep-buckets.py` — read the wrapper's sweep buckets, so the round stops deriving them.

The wrapper serves `GET /api/1.0/sweep?subject=<name>`: the sweep gate's rows already
classified, straight from the same `sweepgate.ClassifiedRows` the rendered frame uses.
Before this script nothing in `claude-supervisor` called that route at all, so the fleet
round kept re-deriving the classification from the same inputs — two derivations that can
disagree about one row, which is the defect the route exists to remove.

⚠️ **What this script returns is PAGE buckets, not session verdicts.** A page bucket
answers *what state is this record in* (`waiting`, `hold`, `problem`, `done`, …). A session
verdict answers *is this session doing anything* (`progressing`, `parked`, `stalled`, …).
They are different domains and this script does not blur them: it emits a `pages=` layer
and nothing else. A caller that also needs session verdicts keeps its own `sessions=` layer
beside it — one key holding both would be an ambiguous field two readers interpret
differently, which is not a reconcile.

⚠️ **An unrecognised bucket is preserved verbatim.** Counting is keyed on the raw `bucket`
string the wrapper sent, never on a local enum. A value this script has never seen — a
bucket the wrapper added, or a stub's `probe-a` — appears in the output exactly as sent.
Folding an unknown value into `other` would make the output look identical to a round that
had re-derived its own classification, which is the one thing this output must be able to
prove it did not do.

Usage:

  sweep-buckets.py --subject Deploy --subject Fleet
  sweep-buckets.py --subject Deploy --json
  sweep-buckets.py --subject Deploy --base-url http://localhost:18091

The base URL resolves `--base-url`, then `$SUPERVISOR_WRAPPER_URL`, then
`http://localhost:18090` — the same convention `manager-liveness.py`, `deliver-message.py`
and `reap-idle-headless.py` already follow. The override is what lets a probe point the
caller at a second daemon (`claude-wrapper -listen :18091` with its own
`SWEEP_GATE_STATE_DIR`) instead of stubbing the live one. ⚠️ **It must be an ORIGIN, with
no path** — the route is appended to it, so a value carrying a path would collect a
route-level 404 the caller cannot tell from a gate that is down. A redirect is refused
rather than followed, for the same reason `deliver-message.py` refuses one.

Exit codes:

  0  at least one subject answered. A subject that failed is reported in `failed=` and is
     never silently dropped — a partial read that reads as a complete one is the same
     false-quiet this task exists to remove. ⚠️ **Counting is per READ, not per distinct
     subject:** `--subject X --subject X` is two reads, so two failures report `failed=2`
     and exit 1. Keying on the subject name would collapse the repeat and return 0 on a
     total failure.
  1  every read failed (the wrapper is down, or the state dir is unpopulated).
  2  usage error: no subject, a subject carrying a path separator or `..`, or a base URL
     that is not an origin.

⚠️ **A 503 is an ANSWER, not a failure to retry.** The route refuses with 503 when the
tick has not written rows yet, or when the cached rows are older than three tick intervals.
Both mean "the gate is not currently describing this subject", which the caller must see as
`UNKNOWN` rather than as an empty bucket set — an empty set would read as "nothing is
tracked".
"""
import argparse
import collections
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "http://localhost:18090"

#: Exit codes, named so the caller reads a token rather than a magic number.
EXIT_OK = 0
EXIT_ALL_FAILED = 1
EXIT_USAGE = 2


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect instead of following it.

    `urlopen` follows a 3xx by default, so a proxy or a misrouted port could turn this
    read into a request to somewhere else and still report success. A redirect here means
    the address is not what the caller thinks it is, which is a refusal. Same convention
    as `deliver-message.py`.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def resolve_base_url(cli_value):
    """Return `(origin, None)` or `(None, problem)`.

    ⚠️ **An ORIGIN, with no path.** The route is appended to this value, so a base URL
    carrying a path would hit `<path>/api/1.0/sweep` and collect a route-level 404 — which
    a caller cannot tell from a gate that is down. `rstrip("/")` does not enforce that;
    `urlsplit` does. Same rule as `manager-liveness.py`'s reach probe.
    """
    raw = (cli_value or os.environ.get("SUPERVISOR_WRAPPER_URL", "") or DEFAULT_BASE_URL).strip()
    parts = urllib.parse.urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        return None, "base URL {!r} is not an origin".format(raw)
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None, "base URL {!r} carries a path — the route is appended to an origin".format(raw)
    return "{}://{}".format(parts.scheme, parts.netloc), None


def refuse_unsafe_subject(subject):
    """Mirror the wrapper's own boundary check, so a bad subject fails locally.

    The route answers 400 for these, but a caller reading `UNKNOWN (HTTP 400)` cannot tell
    a bad argument from a gate that is down. Refusing here keeps the two apart.
    """
    if not subject:
        return "empty subject"
    if ".." in subject:
        return "subject carries '..'"
    if "/" in subject or "\\" in subject:
        return "subject carries a path separator"
    return None


def fetch_rows(base_url, subject, timeout=10.0):
    """Return `(rows, None)` on 200, `(None, reason)` on anything else.

    `reason` is a short token the caller renders; it never raises, because one dead
    subject must not abort the whole read.
    """
    url = "{}/api/1.0/sweep?subject={}".format(
        base_url, urllib.parse.quote(subject, safe="")
    )
    try:
        with _OPENER.open(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        return None, "HTTP {}: {}".format(exc.code, detail[:200] if detail else exc.reason)
    except urllib.error.URLError as exc:
        return None, "unreachable: {}".format(exc.reason)
    except OSError as exc:  # timeouts surface here on some Python builds
        return None, "unreachable: {}".format(exc)

    try:
        parsed = json.loads(body)
    except ValueError as exc:
        return None, "unparseable body: {}".format(exc)

    if not isinstance(parsed, list):
        return None, "body is not a row list"

    rows = []
    for entry in parsed:
        if not isinstance(entry, dict):
            return None, "row is not an object"
        bucket = entry.get("bucket")
        if not isinstance(bucket, str) or not bucket:
            # A row with no bucket is a row the gate did not classify. Counting it under
            # a made-up key would invent a bucket; refusing names the defect instead.
            return None, "row without a bucket: {}".format(entry.get("name", "<unnamed>"))
        rows.append(
            {
                "name": entry.get("name", ""),
                "session": entry.get("session", ""),
                "bucket": bucket,
                "label": entry.get("label", ""),
                "status": entry.get("status", ""),
                "phase": entry.get("phase", ""),
            }
        )
    return rows, None


def count_buckets(rows):
    """Count by the RAW bucket string, sorted for a deterministic line."""
    counts = collections.Counter(row["bucket"] for row in rows)
    return collections.OrderedDict(sorted(counts.items()))


def render_pages_line(counts):
    """`pages=<k>=<n> …`, keys sorted — the shape the round echoes into `.round`."""
    if not counts:
        return "pages="
    return "pages=" + " ".join("{}={}".format(k, v) for k, v in counts.items())


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read the wrapper's sweep buckets for one or more subjects.",
    )
    parser.add_argument(
        "--subject",
        action="append",
        default=[],
        metavar="NAME",
        help="subject to read; repeatable, at least one required",
    )
    parser.add_argument(
        "--base-url",
        default="",
        metavar="URL",
        help="wrapper base URL (default: $SUPERVISOR_WRAPPER_URL or %s)" % DEFAULT_BASE_URL,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of the text line",
    )
    args = parser.parse_args(argv)

    if not args.subject:
        print("no-subject: pass at least one --subject", file=sys.stderr)
        return EXIT_USAGE

    for subject in args.subject:
        problem = refuse_unsafe_subject(subject)
        if problem:
            print("bad-subject: {} ({!r})".format(problem, subject), file=sys.stderr)
            return EXIT_USAGE

    base_url, problem = resolve_base_url(args.base_url)
    if problem:
        print("bad-base-url: {}".format(problem), file=sys.stderr)
        return EXIT_USAGE

    merged = collections.Counter()
    # ⚠️ Both collections are LISTS keyed by occurrence, never dicts keyed by subject.
    # A dict collapses a repeated `--subject`, so `--subject X --subject X` with both
    # reads failing would leave one failure entry against two requested reads and return
    # EXIT_OK on a total failure — the false-quiet this script exists to prevent.
    results = []
    failures = []
    answered = 0

    for subject in args.subject:
        rows, reason = fetch_rows(base_url, subject)
        if reason is not None:
            failures.append({"subject": subject, "reason": reason})
            results.append({"subject": subject, "error": reason})
            continue
        counts = count_buckets(rows)
        merged.update(counts)
        answered += 1
        results.append({"subject": subject, "buckets": dict(counts), "rows": rows})

    if args.json:
        payload = {
            "base_url": base_url,
            "pages": dict(sorted(merged.items())),
            "failed": failures,
            "subjects": results,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_pages_line(collections.OrderedDict(sorted(merged.items()))))
        for failure in failures:
            print("failed={} reason={}".format(failure["subject"], failure["reason"]))
        if failures:
            print("failed={}".format(len(failures)))

    if answered == 0:
        return EXIT_ALL_FAILED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
