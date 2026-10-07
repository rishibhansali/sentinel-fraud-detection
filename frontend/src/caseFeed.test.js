import assert from 'node:assert/strict'
import test from 'node:test'
import { applyCaseEvent, scanCases, visibleCases, caseCounts } from './caseFeed.js'

const row = (id, priority_score, status = 'open') => ({
  id, transaction_id: 300000000 + id, user_id: -7000 - id,
  priority_score, status, fired_rules: ['velocity'],
})

test('queue order follows backend priority and id tie-break', () => {
  const cases = [row(1, 0.4), row(2, 0.8), row(3, 0.8)]
  assert.deepEqual(visibleCases(cases, 'pending', '', 50).map(item => item.id), [3, 2, 1])
})

test('status and search filters include only matching cases', () => {
  const cases = [row(1, 0.4), row(2, 0.8, 'in_review'), row(3, 0.9, 'false_positive')]
  assert.deepEqual(visibleCases(cases, 'open', '', 50).map(item => item.id), [1])
  assert.deepEqual(visibleCases(cases, 'all', '300000003', 50).map(item => item.id), [3])
  assert.deepEqual(visibleCases(cases, 'pending', '-7002', 50).map(item => item.id), [2])
  assert.deepEqual(caseCounts(cases), { open: 1, in_review: 1, decided: 1, total: 3 })
})

test('full paging applies live updates buffered during the scan', async () => {
  const pending = []
  const fetchPage = async sinceId => {
    if (sinceId === 0) return { items: [row(1, 0.4)], next_since_id: 1 }
    pending.push({ type: 'case.updated', case: row(1, 0.9, 'in_review') })
    pending.push({ type: 'case.created', case: row(3, 0.6) })
    return { items: [row(2, 0.5)], next_since_id: null }
  }
  const cases = await scanCases(fetchPage, pending)
  assert.deepEqual([...cases.values()].map(item => [item.id, item.status]), [
    [1, 'in_review'], [2, 'open'], [3, 'open'],
  ])
  assert.equal(cases.get(1).priority_score, 0.9)
  assert.equal(pending.length, 0)
})

test('case events replace an existing id and ignore non-case messages', () => {
  const current = new Map([[1, row(1, 0.4)]])
  applyCaseEvent(current, { type: 'case.updated', case: row(1, 0.7, 'in_review') })
  applyCaseEvent(current, { type: 'heartbeat' })
  assert.equal(current.size, 1)
  assert.equal(current.get(1).status, 'in_review')
})

test('scan rejects a cursor that does not advance', async () => {
  await assert.rejects(
    scanCases(async () => ({ items: [row(1, 0.2)], next_since_id: 0 }), []),
    /cursor did not advance/,
  )
})
