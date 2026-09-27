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
export async function fetchOpenCards({ storeUrl, sessionId, fetchImpl = globalThis.fetch, timeoutMs = 3000 }) {
  if (!storeUrl || !sessionId) return null
  try {
    const res = await fetchImpl(`${storeUrl}/api/1.0/attention`, { signal: AbortSignal.timeout(timeoutMs) })
    if (!res.ok) return null
    const data = await res.json()
    const items = Array.isArray(data) ? data : data?.items ?? data?.data
    if (!Array.isArray(items)) return null
    return openCardsOf(items, sessionId)
  } catch {
    return null
  }
}
