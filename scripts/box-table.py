#!/usr/bin/env python3
"""Render a box-drawn table (used by topic-manager / topic-status / fleet-manager sweeps).

stdin: JSON {"header": [...], "rows": [[...]], "widths": [...]}  (widths optional, default 20)

Columns are separated in the border rows (┬ ┼ ┴), header and cells are both
left-aligned, and over-long cells truncate with '…'.

Padding is computed on DISPLAY width, not len(): the bucket icons used in the
Status column (🔄 ⌛ ⏸️ ✅ ⚠️ 🚀) occupy two terminal cells but count as one
character in Python, so len()-based padding leaves every row with an icon one
cell short and the right-hand border ragged.
"""
import sys, json, unicodedata

data = json.load(sys.stdin)
hdr = data["header"]
rows = data["rows"]
widths = data.get("widths") or [20] * len(hdr)


def cwidth(ch):
    """Display cells occupied by one character."""
    if ch == "️":  # variation selector-16: zero-width, but widens what precedes it
        return 0
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def dwidth(s):
    """Display width of a string, counting VS16-presentation emoji as wide."""
    total = 0
    for i, ch in enumerate(s):
        w = cwidth(ch)
        # `⚠` and `⏸` are Neutral on their own but render wide with VS16 after them
        if w == 1 and i + 1 < len(s) and s[i + 1] == "️":
            w = 2
        total += w
    return total


def fmt(s, w):
    s = str(s)
    if dwidth(s) > w:
        out = ""
        for ch in s:
            if dwidth(out) + cwidth(ch) > w - 1:
                break
            out += ch
        s = out + "…"
    return s + " " * (w - dwidth(s))


def line(vals):
    return "│ " + " │ ".join(fmt(v, w) for v, w in zip(vals, widths)) + " │"


def rule(left, mid, right):
    return left + mid.join("─" * (w + 2) for w in widths) + right


print(rule("┌", "┬", "┐"))
print(line(hdr))
print(rule("├", "┼", "┤"))
for r in rows:
    print(line(r))
print(rule("└", "┴", "┘"))
