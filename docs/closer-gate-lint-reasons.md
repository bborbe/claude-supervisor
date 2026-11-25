# `closer-gate-lint.py` reasons — which ones mean "session-bound observable"

The canonical classification behind the daily SC4 reading
(`scripts/closer-daily-reading.py`). SC4 reads *"no worker closer's `⏰ Ends:`
observable is a session-bound mechanism (background sub-agent, `Monitor`,
`run_in_background` shell, `ScheduleWakeup`, `CronCreate`) over a 7-day
window"*, so the reading has to know exactly which record values answer that
question. Everything below is read from `scripts/closer-gate-lint.py` as it
stands at v0.367.6; line numbers drift between releases, the reason strings do
not.

## Exactly one reason is session-bound

`session-bound-ends` (line 825) is the **only** value that can carry a `class`,
and its five possible classes are `SESSION_BOUND` (line 370), matched by
`session_bound_class()` (line 460):

```python
SESSION_BOUND = (
    ("background-sub-agent", re.compile(
        r"\b(?:background\s+(?:sub-?)?agents?|sub-?agents?|agent\s+tasks?)\b", re.IGNORECASE)),
    ("monitor", re.compile(r"(?<![\w/.-])Monitor(?![\w/.-])")),
    ("run-in-background-shell", re.compile(
        r"\brun_in_background\b|\bbackground\s+(?:bash|shell)s?\b", re.IGNORECASE)),
    ("schedule-wakeup", re.compile(r"\bScheduleWakeup\b", re.IGNORECASE)),
    ("cron-create", re.compile(r"\bCronCreate\b", re.IGNORECASE)),
)
```

Each of the five is a direct match to one mechanism SC4 names, which is why the
mapping is one-to-one rather than a judgement call:

| class | SC4 mechanism | pattern |
|---|---|---|
| `background-sub-agent` | background sub-agent | `\b(?:background\s+(?:sub-?)?agents?\|sub-?agents?\|agent\s+tasks?)\b` |
| `monitor` | `Monitor` | `(?<![\w/.-])Monitor(?![\w/.-])` — case-sensitive and standalone, so a path segment `monitor-check/` is not a match |
| `run-in-background-shell` | `run_in_background` shell | `\brun_in_background\b\|\bbackground\s+(?:bash\|shell)s?\b` |
| `schedule-wakeup` | `ScheduleWakeup` | `\bScheduleWakeup\b` |
| `cron-create` | `CronCreate` | `\bCronCreate\b` — the tool only; a Kubernetes CronJob is external and durable |

⚠️ `session_bound_class()` returns `None` when the `⏰ Ends:` body matches any
`DURABLE` class first (line 419: file, card-id, liveness-probe, task-field-read,
clock, external-probe). That ordering is the whole point: a closer naming a
file mtime is durable even if it also mentions a background agent, so a
`session-bound-ends` record means the observable had **no** durable anchor.

## Every other reason is not

| reason | emitting line(s) | what it means |
|---|---|---|
| `not-a-gate` | 895 | the last turn's `👤 You:` is not an ask. ⚠️ It still carries the `closer` text and is counted in the daily reading's `closers=`: the session-bound check (821) runs before this one (895), so its `⏰ Ends:` was judged. Measured 2026-11-24: 2 672 of 3 324 |
| `no-closer` | 814 | no `👤 You:` line found at all — the only reason whose `closer` is null |
| `not-allow-listed` | 899 | the verb is outside the allowed set |
| `allow-listed` | 900 | the closer passed every check (the only clean outcome) |
| `stop-hook-active` | 735, 738, 742, 744, 752 | a continued turn; allows so the bound stays finite |
| `blocked-retry` | 753 | the same closer was re-blocked once already. ⚠️ It **re-emits the original `class`**, which is why the daily reading counts distinct sessions, not records |
| `kill-switch` | 800 | `SUPERVISOR_CLOSER_GATE_LINT=off` |
| `no-transcript-path` | 808 | the payload carried no transcript path |
| `transcript-unreadable` | 812 | the transcript could not be read |
| `unqualified-pr-reference` | 838 | a `PR #N` with no repository, so it can never be re-probed |
| `pr-reference-without-number` | 851 | a PR reference with no number |
| `paneless-pane-only-ask` | 862 | a headless session asked a pane-only question |
| `unpollable-handle` | 869 | **not session-bound.** `unpollable_handles()` (line 163) keeps card handles whose length ≠ `STORE_ITEM_ID_LEN` (32) — a card id `poll` cannot address. Nothing here reads a session |
| `absent-card` | 884 | a 32-hex id the store does not hold |
| `ownerless-blocked` | 890 | a `🔴 BLOCKED` closer naming no owner |
| `malformed-stdin` | 1030 | stdin was not JSON — the record a bare shell run writes |
| `non-object` | 1033 | stdin parsed but was not an object |
| `internal-error` | 1040 | the evaluator raised; fail-open, recorded so a crashing guard is visible |

## Why the reading counts sessions, not records

`blocked-retry` is the one reason that copies a `class` onto a record that did
not itself find the observable. Counting records therefore reports a session
twice: on 2026-11-24 there were **3** records with a non-null `class` across
**2** sessions (`monitor` ×2 in one session, `background-sub-agent` ×1 in
another). `scripts/closer-daily-reading.py` reports `session-bound=2`, which is
the number the criterion is about — sessions that ended on a session-bound
observable.
