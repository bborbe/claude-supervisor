#!/usr/bin/env python3
"""Rolling archive for CHANGELOG.md.

`CHANGELOG.md` gains a released section per release and is never pruned, so it grows
without bound — and the maintainer release watcher refuses to read a `CHANGELOG.md`
over 1 MiB, dropping the whole repo from every poll cycle, so no release can be cut at
all. Measured 2026-10-13 on `bborbe/claude-supervisor`: the file crossed 1 MiB and the
watcher logged `repo dropped from cycle … too large` on every cycle while the releaser
pod never ran.

The archive keeps the newest released sections in `CHANGELOG.md` up to
`ARCHIVE_TARGET_BYTES` and moves everything older into `CHANGELOG-archive.md`, which
nothing reads — the archive has no size limit, only `CHANGELOG.md` does.

Both constants live here and only here. `--check` is the guard wired into
`make precommit`: it fails while there is still runway and names the remedy, so the cap
can never be crossed silently again.

Sections are sliced out of the source byte-for-byte, never re-serialised: a released
section's text is frozen history, and the fold guard byte-compares sections against
their own tags.
"""
import pathlib
import re
import sys

# The watcher's read cap (github-release-watcher pkg/githubclient.go) — hard, not advisory.
WATCHER_LIMIT_BYTES = 1024 * 1024
# Archive down to this much: well under the cap, so one archive buys weeks, not days.
ARCHIVE_TARGET_BYTES = 384 * 1024
# Fail above this — 75% of the cap, leaving runway to archive before the watcher drops.
MAX_BYTES = 768 * 1024
# Never archive below this many released sections, however long the entries grow.
MIN_KEPT_RELEASES = 20

CHANGELOG = "CHANGELOG.md"
ARCHIVE = "CHANGELOG-archive.md"
RELEASED = re.compile(r"^## v\d+\.\d+\.\d+$")
POINTER = re.compile(r"^_Releases older than .*archived in \[CHANGELOG-archive\.md\]\(CHANGELOG-archive\.md\)\._$")

ARCHIVE_HEADER = """# Changelog Archive

Released sections rotated out of [CHANGELOG.md](CHANGELOG.md) so that file stays under
the release watcher's 1 MiB read cap. Frozen history — newest first, verbatim, never
edited. The newest releases live in [CHANGELOG.md](CHANGELOG.md).

"""


def headings(text):
    """[(heading, offset)] for every `## ` heading, in file order."""
    return [(m.group(0), m.start()) for m in re.finditer(r"^## .*$", text, re.M)]


def without_pointer(text):
    """The file with a previously written pointer line (and the blanks around it) removed."""
    lines = [l for l in text.split("\n") if not POINTER.match(l)]
    while len(lines) > 1 and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def archive_body(text):
    """The released sections of an existing archive, from its first `## v` heading.

    The header is re-emitted from `ARCHIVE_HEADER`, so the prior body is taken from the first
    release heading rather than from a header shape that may have drifted.
    """
    m = re.search(r"^## v\d+\.\d+\.\d+$", text, re.MULTILINE)
    return text[m.start():] if m else ""


def merge_archive(existing, archived):
    """Newly archived sections first, then the prior archive's — newest first, no duplicates.

    ⚠️ **The prior archive is not optional.** This rotation appends to frozen history; writing
    only the freshly cut suffix silently deletes every section archived by an earlier run, and
    nothing else in the tree holds them. Observed 2026-10-29: a second rotation overwrote
    `CHANGELOG-archive.md` and dropped 316 released sections (v0.102.3 … v0.1.0) out of the
    repository entirely. A heading already present in `archived` is skipped so a repeated run
    cannot duplicate it.
    """
    seen = {line for line in archived.split("\n") if RELEASED.match(line)}
    kept = []
    for block in re.split(r"(?m)^(?=## v\d+\.\d+\.\d+$)", archive_body(existing)):
        head = block.split("\n", 1)[0]
        if RELEASED.match(head) and head not in seen:
            kept.append(block)
            seen.add(head)
    return archived + "".join(kept)


def main(argv):
    root = pathlib.Path(__file__).resolve().parent.parent
    path = root / CHANGELOG
    text = path.read_text(encoding="utf-8")
    size = len(text.encode("utf-8"))

    if "--check" in argv:
        if size > MAX_BYTES:
            print(
                f"CHANGELOG.md is {size} bytes, over the {MAX_BYTES}-byte guard. The release "
                f"watcher drops the whole repo at {WATCHER_LIMIT_BYTES} bytes, so no release can "
                f"be cut. Run `make changelog-archive`.",
                file=sys.stderr,
            )
            return 1
        print(f"  changelog size ok: {size} bytes (guard {MAX_BYTES}, watcher cap {WATCHER_LIMIT_BYTES})")
        return 0

    text = without_pointer(text)
    heads = headings(text)
    if not heads:
        print("CHANGELOG.md has no `## ` heading — nothing to archive", file=sys.stderr)
        return 1

    kept_releases, total = 0, len(text[: heads[0][1]].encode("utf-8"))
    cut = len(text)
    for i, (heading, offset) in enumerate(heads):
        if heading == "## Unreleased":
            continue
        if not RELEASED.match(heading):
            print(f"refusing: unrecognised section heading {heading!r}", file=sys.stderr)
            return 1
        end = heads[i + 1][1] if i + 1 < len(heads) else len(text)
        section = len(text[offset:end].encode("utf-8"))
        if kept_releases >= MIN_KEPT_RELEASES and total + section > ARCHIVE_TARGET_BYTES:
            cut = offset
            break
        total += section
        kept_releases += 1

    if cut == len(text):
        print(f"  changelog archive: nothing to archive ({kept_releases} released section(s), {size} bytes)")
        return 0

    kept, archived = text[:cut].rstrip("\n"), text[cut:]
    oldest_kept = headings(kept)[-1][0].removeprefix("## ")
    archived_heads = [h.removeprefix("## ") for h, _ in headings(archived)]
    pointer = (
        f"\n_Releases older than {archived_heads[0]} ({archived_heads[-1]} … "
        f"{archived_heads[0]}) are archived in [CHANGELOG-archive.md]({ARCHIVE})._\n"
    )
    path.write_text(f"{kept}\n{pointer}", encoding="utf-8")
    archive_path = root / ARCHIVE
    prior = archive_path.read_text(encoding="utf-8") if archive_path.exists() else ""
    archive_path.write_text(ARCHIVE_HEADER + merge_archive(prior, archived), encoding="utf-8")

    new_size = len(path.read_text(encoding="utf-8").encode("utf-8"))
    print(
        f"  changelog archive: CHANGELOG.md {size} → {new_size} bytes; kept {kept_releases} "
        f"released section(s) down to {oldest_kept}; moved {len(archived_heads)} "
        f"({archived_heads[-1]} … {archived_heads[0]}) to {ARCHIVE}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
