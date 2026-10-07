export class ApiError extends Error {
  constructor(status, message) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

export async function request(path, { method = 'GET', body, signal } = {}) {
  const options = { method, signal, headers: {} }
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json'
    options.body = JSON.stringify(body)
  }
  let response
  try {
    response = await fetch(path, options)
  } catch (error) {
    if (error?.name === 'AbortError') throw error
    throw new ApiError(0, 'Cannot reach the Sentinel API. Check that the backend is running.')
  }
  let payload
  try {
    payload = await response.json()
  } catch {
    payload = null
  }
  if (!response.ok) {
    const detail = payload?.detail
    const message = typeof detail === 'string' ? detail : detail?.message
    throw new ApiError(response.status, message || `Request failed (${response.status})`)
  }
  return payload
}

export const getCasePage = (sinceId, signal) =>
  request(`/cases?since_id=${sinceId}&limit=500`, { signal })
export const getCaseDetail = (id, signal) => request(`/cases/${id}`, { signal })
export const claimCase = (id, analyst) =>
  request(`/cases/${id}/claim`, { method: 'POST', body: { analyst } })
export const releaseCase = (id, analyst) =>
  request(`/cases/${id}/release`, { method: 'POST', body: { analyst } })
export const decideCase = (id, analyst, decision, note) =>
  request(`/cases/${id}/feedback`, { method: 'POST', body: { analyst, decision, note: note || null } })
export const getRules = () => request('/rules')
export const getRuleStats = () => request('/stats/rules')
export const getRuleHistory = () => request('/rules/history?limit=20')
export const patchRule = (name, body) =>
  request(`/rules/${encodeURIComponent(name)}`, { method: 'PATCH', body })
