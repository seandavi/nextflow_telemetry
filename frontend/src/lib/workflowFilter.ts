// Options for the Process Metrics workflow name/version filters.
type Wf = { workflow_id: string; version: string }

export function workflowNames(wfs: Wf[]): string[] {
  return [...new Set(wfs.map(w => w.workflow_id))].sort()
}

// Versions registered for one workflow; none until a workflow is picked.
export function workflowVersions(wfs: Wf[], workflowId?: string): string[] {
  if (!workflowId) return []
  return [...new Set(wfs.filter(w => w.workflow_id === workflowId).map(w => w.version))].sort()
}
