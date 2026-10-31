// Self-check for metrics filter persistence and custom-range conversion: `npm run check`.
import assert from 'node:assert/strict'
import { initialFilters, localInputToIso, isoToLocalInput } from '../src/lib/useUrlFilters.ts'

const defaults = { windowDays: 30 }
// URL wins, then this session's last selection, then defaults
assert.deepEqual(initialFilters('?window_hours=6', 'window_days=7', defaults), { windowHours: 6 })
assert.deepEqual(initialFilters('', 'since=2026-01-01T00%3A00%3A00.000Z', defaults), { since: '2026-01-01T00:00:00.000Z' })
assert.deepEqual(initialFilters('', '', defaults), {})  // "All time" persisted as empty
assert.deepEqual(initialFilters('', null, defaults), defaults)

// datetime-local round-trips through UTC ISO
const iso = localInputToIso('2026-03-04T05:06')
assert.match(iso, /^\d{4}-\d\d-\d\dT\d\d:\d\d:00\.000Z$/)
assert.equal(isoToLocalInput(iso), '2026-03-04T05:06')
assert.equal(localInputToIso(''), undefined)
assert.equal(isoToLocalInput(undefined), '')
console.log('filters check ok')
