// Self-check for the study status rule and CSV parser: `npm run check`.
// Node ≥ 22.18 strips the TypeScript types from studies.ts on import.
import assert from 'node:assert/strict'
import { parseCsv, toCatalog, studyStatus, joinStudies, summarizeStudies } from '../src/lib/studies.ts'

const lb = (o) => ({
  collection_id: 'X', source: 'manual', label: null, sample_count: 0, samples_completed: 0,
  samples_failed: 0, samples_running: 0, samples_remaining: 0, completion_pct: 0, last_completed_at: null, ...o,
})

assert.equal(studyStatus(24, undefined), 'not_started')
assert.equal(studyStatus(24, lb({ sample_count: 24, samples_running: 24 })), 'in_progress')
assert.equal(studyStatus(156, lb({ sample_count: 3, samples_completed: 3 })), 'partial')
assert.equal(studyStatus(3, lb({ sample_count: 3, samples_completed: 3 })), 'processed')
assert.equal(studyStatus(10, lb({ sample_count: 10, samples_completed: 4, samples_running: 2 })), 'in_progress')
assert.equal(studyStatus(10, lb({ sample_count: 10, samples_failed: 10 })), 'not_started')
assert.equal(studyStatus(0, lb({ sample_count: 0 })), 'not_started')
assert.equal(studyStatus(null, lb({ sample_count: 2, samples_completed: 2 })), 'processed')

const csv = 'study_name,n_samples,body_site,notes\r\nA_2020,5,feces;milk,"has, comma"\r\nB_2021,3,feces,"say ""hi"""\r\n\r\n'
const rows = parseCsv(csv)
assert.equal(rows.length, 2)
assert.equal(rows[0].notes, 'has, comma')
assert.equal(rows[1].notes, 'say "hi"')
const cat = toCatalog(rows)
assert.deepEqual(cat[0].body_site, ['feces', 'milk'])

const joined = joinStudies(cat, [lb({ collection_id: 'A_2020', sample_count: 5, samples_completed: 5 }), lb({ collection_id: 'Z_v2only', sample_count: 1, samples_running: 1 })])
assert.deepEqual(joined.map(r => [r.study_name, r.status]), [['A_2020', 'processed'], ['B_2021', 'not_started'], ['Z_v2only', 'in_progress']])
const s = summarizeStudies(joined)
assert.deepEqual(s.counts, { processed: 1, partial: 0, in_progress: 1, not_started: 1 })
assert.equal(s.completed, 5)
assert.equal(s.curated, 8)

console.log('check-studies: ok')
