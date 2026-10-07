const PENDING = new Set(['open', 'in_review'])

export function applyCaseEvent(cases, event) {
  if ((event?.type === 'case.created' || event?.type === 'case.updated') &&
      event.case && Number.isInteger(event.case.id)) {
    cases.set(event.case.id, event.case)
  }
  return cases
}

export async function scanCases(fetchPage, pendingEvents = []) {
  const cases = new Map()
  let sinceId = 0
  while (true) {
    const page = await fetchPage(sinceId)
    if (!Array.isArray(page?.items)) throw new Error('Case reconciliation returned invalid data')
    for (const item of page.items) cases.set(item.id, item)
    if (page.next_since_id == null) break
    if (!Number.isInteger(page.next_since_id) || page.next_since_id <= sinceId) {
      throw new Error('Case reconciliation cursor did not advance')
    }
    sinceId = page.next_since_id
  }
  for (const event of pendingEvents) applyCaseEvent(cases, event)
  pendingEvents.length = 0
  return cases
}

export function caseCounts(cases) {
  const counts = { open: 0, in_review: 0, decided: 0, total: cases.length }
  for (const item of cases) {
    if (item.status === 'open') counts.open += 1
    else if (item.status === 'in_review') counts.in_review += 1
    else counts.decided += 1
  }
  return counts
}

export function visibleCases(cases, status, query, limit = 50) {
  const needle = query.trim().toLowerCase()
  return cases
    .filter(item => status === 'all' ||
      (status === 'pending' ? PENDING.has(item.status) : item.status === status))
    .filter(item => !needle || String(item.transaction_id).includes(needle) ||
      String(item.user_id).includes(needle) || String(item.id).includes(needle))
    .sort((a, b) => b.priority_score - a.priority_score || b.id - a.id)
    .slice(0, limit)
}
