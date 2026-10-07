import assert from 'node:assert/strict'
import test from 'node:test'
import { buildRulePatch, makeRuleDraft } from './ruleForm.js'

const rule = {
  rule_name: 'velocity', weight: 1, enabled: true,
  params: { window_minutes: 10, threshold_count: 5 },
}

test('unchanged rule produces no PATCH', () => {
  assert.equal(buildRulePatch(rule, makeRuleDraft(rule), 'Asha'), null)
})

test('only changed typed fields are sent', () => {
  const draft = makeRuleDraft(rule)
  draft.enabled = false
  draft.weight = '1.5'
  draft.params.threshold_count = '4'
  assert.deepEqual(buildRulePatch(rule, draft, ' Asha '), {
    changed_by: 'Asha', enabled: false, weight: 1.5,
    params: { threshold_count: 4 },
  })
})

test('fractional integer parameter and missing analyst are rejected', () => {
  const draft = makeRuleDraft(rule)
  draft.params.threshold_count = '4.5'
  assert.throws(() => buildRulePatch(rule, draft, 'Asha'), /whole number/)
  draft.params.threshold_count = '4'
  assert.throws(() => buildRulePatch(rule, draft, '  '), /analyst name/)
})

test('decimal parameters accept fractional edits from integer-valued defaults', () => {
  const amount = {
    rule_name: 'amount_baseline', weight: 1, enabled: true,
    params: { deviation_multiplier: 3 },
  }
  const draft = makeRuleDraft(amount)
  draft.params.deviation_multiplier = '3.5'
  assert.deepEqual(buildRulePatch(amount, draft, 'Asha'), {
    changed_by: 'Asha', params: { deviation_multiplier: 3.5 },
  })
})

test('nonfinite and negative weight are rejected', () => {
  const draft = makeRuleDraft(rule)
  draft.weight = '-1'
  assert.throws(() => buildRulePatch(rule, draft, 'Asha'), /Weight/)
  draft.weight = 'Infinity'
  assert.throws(() => buildRulePatch(rule, draft, 'Asha'), /Weight/)
})
