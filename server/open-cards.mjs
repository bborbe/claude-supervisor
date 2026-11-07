// open-cards.mjs — a tab worker's open attention cards, for `agent_status`.
//
// The problem this solves: a tab worker's permission prompt never enters the park queue
// (`makeCanUseTool` is wired only for headless workers), so `pending_permissions` is empty
// for it by construction and a manager reading `agent_status` cannot see the gate. The
// attention store DOES carry it — `attention-watcher.py` pushes every feed item there,
// keyed by the worker's session id — and since v0.60.0 the plugin's PermissionRequest hook
// carries an arm answer on that card into the live prompt. So the store card is both the
// thing to read and the thing to answer; this module is the read.
//
// What this module owns is the *rule*: given the store's item list, which cards are this
// session's open ones? That is pure and tested on its own. The fetch is a thin wrapper.

// The fields a manager needs to decide and to answer: `item_id` is what
// `attention-answer.py answer <item_id>` takes, `answer_mechanism` says whether that is a
// permission release or a message, `payload` is the gate's own subject.
export function openCardsOf(items, sessionId) {
  if (!sessionId || !Array.isArray(items)) return []
  return items
    .filter((i) => i && typeof i === 'object' && i.producer_id === sessionId && i.state === 'open')
    .sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)))
    .map((i) => ({
      item_id: i.item_id,
      answer_mechanism: i.answer_mechanism ?? null,
      payload: typeof i.payload === 'string' ? i.payload.slice(0, 500) : null,
      created_at: i.created_at ?? null,
    }))
}

// `null` means the store could not be read — a different fact from `[]`, which means it
// was read and holds nothing open for this session. Collapsing the two would report a
// blocked worker as unblocked whenever the store is down, which is the false negative this
// field exists to remove.
// The store's whole item list, or `null` when the read could not run.
//
// Exported because a caller reading MANY sessions must pay one round-trip, not one per
// session: `list_agents` maps over the entire roster, so a `fetchOpenCards` per worker
// would be N sequential requests against a 3 s timeout each — and that read sits in front
// of every manager sweep. With the store down that is the difference between a slow answer
// and an unusable one. `openCardsOf` then does the per-session filter, which is pure and
// already tested on its own.
//
// The same `null`-vs-`[]` rule as `fetchOpenCards` holds here, one level up: `null` is "the
// store could not be read", `[]` is "read, and the fleet holds nothing".
export async function fetchAttentionItems({ storeUrl, fetchImpl = globalThis.fetch, timeoutMs = 3000 }) {
  if (!storeUrl) return null
  try {
    const res = await fetchImpl(`${storeUrl}/api/1.0/attention`, { signal: AbortSignal.timeout(timeoutMs) })
    if (!res.ok) return null
    const data = await res.json()
    const items = Array.isArray(data) ? data : data?.items ?? data?.data
    if (!Array.isArray(items)) return null
    return items
  } catch {
    return null
  }
}

// `null` means the store could not be read — a different fact from `[]`, which means it
// was read and holds nothing open for this session. Collapsing the two would report a
// blocked worker as unblocked whenever the store is down, which is the false negative this
// field exists to remove.
export async function fetchOpenCards({ storeUrl, sessionId, fetchImpl = globalThis.fetch, timeoutMs = 3000 }) {
  if (!storeUrl || !sessionId) return null
  const items = await fetchAttentionItems({ storeUrl, fetchImpl, timeoutMs })
  // `null` stays `null` — a failed read must not be laundered into "nothing open for this
  // session" by `openCardsOf`, which would answer `[]` for an unreadable store.
  if (items === null) return null
  return openCardsOf(items, sessionId)
}
