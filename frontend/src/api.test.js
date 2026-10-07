import assert from 'node:assert/strict'
import test from 'node:test'
import { ApiError, request } from './api.js'

test('POST serializes JSON without changing the backend path', async () => {
  const previous = globalThis.fetch
  let seen
  globalThis.fetch = async (path, options) => {
    seen = { path, options }
    return { ok: true, status: 200, json: async () => ({ status: 'in_review' }) }
  }
  try {
    const result = await request('/cases/7/claim', { method: 'POST', body: { analyst: 'Asha' } })
    assert.equal(result.status, 'in_review')
    assert.equal(seen.path, '/cases/7/claim')
    assert.equal(seen.options.headers['Content-Type'], 'application/json')
    assert.deepEqual(JSON.parse(seen.options.body), { analyst: 'Asha' })
  } finally {
    globalThis.fetch = previous
  }
})

test('409 conflict exposes the backend reason', async () => {
  const previous = globalThis.fetch
  globalThis.fetch = async () => ({
    ok: false, status: 409,
    json: async () => ({ detail: { message: 'case is not open', current_status: 'in_review' } }),
  })
  try {
    await assert.rejects(request('/cases/7/claim', { method: 'POST', body: { analyst: 'Asha' } }),
      error => error instanceof ApiError && error.status === 409 && error.message === 'case is not open')
  } finally {
    globalThis.fetch = previous
  }
})
