// Which WezTerm window a spawned tab opens in, resolved from the caller's argument.
//
// Split out of supervisor.mjs for the usual reason (importing supervisor.mjs starts the
// MCP server), and it exists at all because of a failure measured 2026-09-20. The
// argument was accepted only when it arrived as a STRING:
//
//   windowId: typeof args.window_id === 'string' ? args.window_id : undefined
//
// A caller passing the number `0` failed that test, so the flag was dropped entirely and
// the tab inherited the caller's window instead. The result reads as success: the spawn
// opens, the colour is right, and only the window is wrong — the partial-spawn shape the
// role routing exists to eliminate.
//
// The three observations that localised it, same caller window throughout:
//
//   window_id="0"   -> inherited the caller's window          (lost)
//   window_id="00"  -> routed correctly to window 0           (survived)
//   window_id="99"  -> wezterm refused it, "window_id 99 not found"
//
// The third is the one that makes it conclusive: wezterm was being handed the flag and
// parsing it, so the loss was upstream of wezterm and specific to the canonical-number
// spelling. That "00" reaches window 0 at all is why this is a widening rather than a
// workaround — it proves the target is reachable and the value is delivered, leaving the
// gate as the only thing to fix.
//
// The widening must not lose what the original gate protected: an ABSENT window id has to
// stay absent, or every caller that passed nothing would silently be routed to window 0.

// `undefined` and `null` mean "no preference" and survive as such. Everything else —
// including the number 0 and the string "0" — is a real target and is coerced to the
// string wezterm's CLI expects.
//
// Empty and whitespace-only values are deliberately NOT resolved here: they reach
// spawnInteractiveAgent's `String(windowId).trim() !== ''` guard, which drops the flag.
// One place decides emptiness, not two.
export const windowIdArgument = (raw) =>
  raw === undefined || raw === null ? undefined : String(raw)
