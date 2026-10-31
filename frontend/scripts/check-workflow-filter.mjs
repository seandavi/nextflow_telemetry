// Self-check for the Process Metrics workflow/version filter options: `npm run check`.
import assert from 'node:assert/strict'
import { workflowNames, workflowVersions } from '../src/lib/workflowFilter.ts'

const wfs = [
  { workflow_id: 'b', version: '1.0' },
  { workflow_id: 'a', version: '2.0' },
  { workflow_id: 'a', version: '1.0' },
  { workflow_id: 'a', version: '2.0' },
]
assert.deepEqual(workflowNames(wfs), ['a', 'b'])
assert.deepEqual(workflowVersions(wfs, 'a'), ['1.0', '2.0'])
assert.deepEqual(workflowVersions(wfs, 'b'), ['1.0'])
assert.deepEqual(workflowVersions(wfs, undefined), [])
assert.deepEqual(workflowVersions(wfs, 'missing'), [])
console.log('workflow filter checks passed')
