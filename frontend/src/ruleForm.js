export function makeRuleDraft(rule) {
  return {
    enabled: rule.enabled,
    weight: String(rule.weight),
    params: Object.fromEntries(Object.entries(rule.params).map(([key, value]) => [key, String(value)])),
  }
}

function numeric(raw, label, integer = false) {
  if (String(raw).trim() === '') throw new Error(`${label} is required`)
  const value = Number(raw)
  if (!Number.isFinite(value)) throw new Error(`${label} must be a finite number`)
  if (integer && !Number.isInteger(value)) throw new Error(`${label} must be a whole number`)
  return value
}

export function buildRulePatch(rule, draft, analyst) {
  const changedBy = analyst.trim()
  if (!changedBy) throw new Error('Enter an analyst name before saving a rule')
  const body = { changed_by: changedBy }
  if (typeof draft.enabled !== 'boolean') throw new Error('Enabled must be true or false')
  if (draft.enabled !== rule.enabled) body.enabled = draft.enabled

  const weight = numeric(draft.weight, 'Weight')
  if (weight < 0) throw new Error('Weight must be at least zero')
  if (weight !== rule.weight) body.weight = weight

  const changedParams = {}
  for (const [key, original] of Object.entries(rule.params)) {
    const value = numeric(draft.params[key], key.replaceAll('_', ' '), Number.isInteger(original))
    if (value !== original) changedParams[key] = value
  }
  if (Object.keys(changedParams).length) body.params = changedParams
  return Object.keys(body).length > 1 ? body : null
}
