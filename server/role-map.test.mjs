// Unit tests for role → {chip, window_id} resolution.
//
// The case that matters most is window 0. It is the only falsy id in the map, it belongs
// to the manager role — the exact spawn this feature exists to fix — and it is the id the
// caller-side path drops. So "0 is a target, not an absence" is asserted directly rather
// than left implied by the other cases.
//
// The second case that matters is the type. The map writes ids as JSON numbers and this
// module must pass them through unchanged: coercing to a string here would rebuild the
// round-trip the module exists to delete.
//
// The two failure modes are asserted separately, because they are deliberately different:
// an unknown ROLE refuses (a caller mistake), an unusable MAP degrades (the map publishes
// on a reconcile tick, and a headless worker has no window to route).

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { resolveRole, ROLES, DEFAULT_ROLE } from './role-map.mjs'

// The live map's exact shape, read off ~/.cache/wezterm-role-map.json — including the
// fact that the ids are numbers and that `Hold` carries a null chip.
const MAP = {
  Managers: { chip: 'orange', window_id: 0, scheme: 'Gruvbox Material (Gogh)' },
  Direct: { chip: 'cyan', window_id: 1, scheme: 'Solarized Dark (Gogh)' },
  Agents: { chip: 'pink', window_id: 2, scheme: 'Tokyo Night Storm' },
  Hold: { chip: null, window_id: 5, scheme: 'nord' },
}

const pick = (r) => ({ purpose: r.purpose, chip: r.chip, windowId: r.windowId })

test('an absent role resolves as agent, the documented default', () => {
  for (const absent of [undefined, null, '']) {
    const r = resolveRole({ role: absent, map: MAP })
    assert.equal(r.error, undefined)
    assert.equal(r.role, DEFAULT_ROLE)
    assert.equal(r.chip, 'pink')
    assert.equal(r.windowId, 2)
  }
})

test('each role resolves to its own purpose, chip and window', () => {
  assert.deepEqual(pick(resolveRole({ role: 'manager', map: MAP })), {
    purpose: 'Managers',
    chip: 'orange',
    windowId: 0,
  })
  assert.deepEqual(pick(resolveRole({ role: 'agent', map: MAP })), {
    purpose: 'Agents',
    chip: 'pink',
    windowId: 2,
  })
  assert.deepEqual(pick(resolveRole({ role: 'human', map: MAP })), {
    purpose: 'Direct',
    chip: 'cyan',
    windowId: 1,
  })
})

test('the three roles are distinct in BOTH colour and window', () => {
  // "Distinct" has to hold on both axes, or a role is identifiable by one signal only.
  const resolved = ROLES.map((role) => resolveRole({ role, map: MAP }))
  assert.equal(new Set(resolved.map((r) => r.chip)).size, ROLES.length)
  assert.equal(new Set(resolved.map((r) => r.windowId)).size, ROLES.length)
})

test('window 0 is a real target, not an absence', () => {
  const manager = resolveRole({ role: 'manager', map: MAP })
  assert.equal(manager.windowId, 0)
  assert.notEqual(manager.windowId, null)
  assert.notEqual(manager.windowId, undefined)
  assert.equal(manager.resolved, true)
})

test("the map's own type is preserved — ids stay numbers", () => {
  assert.equal(typeof resolveRole({ role: 'manager', map: MAP }).windowId, 'number')
  assert.equal(typeof resolveRole({ role: 'agent', map: MAP }).windowId, 'number')
})

test('a resolved role carries no warning', () => {
  for (const role of ROLES) {
    assert.equal(resolveRole({ role, map: MAP }).warning, null)
  }
})

test('an unknown role refuses rather than guessing', () => {
  const r = resolveRole({ role: 'wizard', map: MAP })
  assert.ok(r.error, 'an unknown role must produce an error')
  assert.match(r.error, /wizard/)
  assert.match(r.error, /manager, agent, human/)
  assert.equal(r.windowId, undefined)
})

test('an unusable map degrades rather than refusing — a headless spawn has no window', () => {
  for (const unusable of [undefined, null, 'a string', 42, []]) {
    const r = resolveRole({ role: 'manager', map: unusable })
    assert.equal(r.error, undefined, `${JSON.stringify(unusable)} must not refuse the spawn`)
    assert.equal(r.resolved, false)
    assert.equal(r.windowId, null)
    assert.ok(r.warning, 'a degradation must be reportable, not silent')
  }
})

test('a map missing one purpose degrades for that role only', () => {
  const partial = { Agents: MAP.Agents }
  assert.equal(resolveRole({ role: 'agent', map: partial }).resolved, true)

  const human = resolveRole({ role: 'human', map: partial })
  assert.equal(human.resolved, false)
  assert.match(human.warning, /Direct/)
})

test('an entry without a window_id is not a usable resolution', () => {
  const r = resolveRole({ role: 'manager', map: { Managers: { chip: 'orange' } } })
  assert.equal(r.resolved, false)
  assert.equal(r.windowId, null)
  assert.match(r.warning, /Managers/)
})

test('a null chip does not by itself invalidate a resolution', () => {
  // `Hold` carries `chip: null` in the live map. A role whose purpose had a window but no
  // chip must still route — the window is the load-bearing half.
  const r = resolveRole({ role: 'manager', map: { Managers: { chip: null, window_id: 0 } } })
  assert.equal(r.resolved, true)
  assert.equal(r.windowId, 0)
  assert.equal(r.chip, null)
})
