import { useCallback, useEffect, useState } from 'react'
import { getRuleHistory, getRules, getRuleStats, patchRule } from '../api.js'
import { formatDate, ruleLabel } from '../format.js'
import { buildRulePatch, makeRuleDraft } from '../ruleForm.js'

function RuleCard({ rule, stats, analyst, onRefresh }) {
  const [draft, setDraft] = useState(() => makeRuleDraft(rule))
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  useEffect(() => { setDraft(makeRuleDraft(rule)) }, [rule])

  async function save(event) {
    event.preventDefault()
    setMessage('')
    setError('')
    let body
    try { body = buildRulePatch(rule, draft, analyst) }
    catch (cause) { setError(cause.message); return }
    if (!body) { setMessage('No changes to save.'); return }
    setSaving(true)
    try {
      const result = await patchRule(rule.rule_name, body)
      setMessage(result.affects_flagging ? 'Saved. This change can affect which future transactions are flagged.' : 'Saved. Queue ranking may change for future cases.')
      await onRefresh()
    } catch (cause) {
      setError(cause.message || 'Could not save this rule')
    } finally {
      setSaving(false)
    }
  }

  const changed = JSON.stringify(draft) !== JSON.stringify(makeRuleDraft(rule))
  const decisions = (stats?.confirmed_fraud || 0) + (stats?.false_positive || 0)

  return <form className="rule-card" onSubmit={save}>
    <div className="rule-card-head">
      <div className="rule-card-title"><span className="rule-symbol" aria-hidden="true">◈</span>
        <div><h3>{ruleLabel(rule.rule_name)}</h3><small>Updated {formatDate(rule.updated_at)}</small></div></div>
      <label className="rule-toggle"><input type="checkbox" checked={draft.enabled}
        onChange={event => setDraft(current => ({ ...current, enabled: event.target.checked }))} />
        <span>{draft.enabled ? 'Enabled' : 'Disabled'}</span></label>
    </div>
    <div className="rule-quality">
      <div><strong>{stats?.precision == null ? '—' : `${Math.round(stats.precision * 100)}%`}</strong><span>precision</span></div>
      <div><strong>{stats?.confirmed_fraud || 0}</strong><span>confirmed</span></div>
      <div><strong>{stats?.false_positive || 0}</strong><span>false positives</span></div>
    </div>
    <p className="rule-quality-note">{decisions ? `Based on ${decisions} decided cases where this rule fired.` : 'No decided cases for this rule yet.'}</p>
    <div className="rule-fields">
      <label>Weight
        <input type="number" min="0" step="any" value={draft.weight}
          onChange={event => setDraft(current => ({ ...current, weight: event.target.value }))} />
      </label>
      {Object.entries(rule.params).map(([key, original]) => <label key={key}>{ruleLabel(key)}
        <input type="number" step={Number.isInteger(original) ? '1' : 'any'}
          value={draft.params[key] ?? ''}
          onChange={event => setDraft(current => ({ ...current,
            params: { ...current.params, [key]: event.target.value },
          }))} />
      </label>)}
    </div>
    <div className="rule-card-foot">
      <span>Weight changes ranking. Enabled state and parameters can change flagging.</span>
      <button className="primary-button" type="submit" disabled={!analyst || saving || !changed}>
        {saving ? 'Saving…' : 'Save changes'}
      </button>
    </div>
    {!analyst && <p className="rule-message">Enter your analyst name above to save changes.</p>}
    {message && <p className="rule-message success" role="status">{message}</p>}
    {error && <p className="inline-alert" role="alert">{error}</p>}
  </form>
}

function historyChange(entry) {
  if (!entry.before) return 'Initial configuration'
  const names = ['enabled', 'weight', 'params'].filter(key =>
    JSON.stringify(entry.before?.[key]) !== JSON.stringify(entry.after?.[key]))
  return names.map(ruleLabel).join(', ') || 'No effective change'
}

export function RulesView({ analyst }) {
  const [rules, setRules] = useState([])
  const [stats, setStats] = useState([])
  const [history, setHistory] = useState([])
  const [version, setVersion] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const [config, quality, audit] = await Promise.all([getRules(), getRuleStats(), getRuleHistory()])
      setRules(config.rules || [])
      setVersion(config.version)
      setStats(quality || [])
      setHistory(audit.history || [])
      setError('')
    } catch (cause) {
      setError(cause.message || 'Could not load rule controls')
      throw cause
    } finally { setLoading(false) }
  }, [])

  useEffect(() => { refresh().catch(() => {}) }, [refresh])
  const statsByName = Object.fromEntries(stats.map(item => [item.rule_name, item]))

  return <div className="rules-view">
    <div className="rules-banner">
      <div><p className="eyebrow">Configuration / Version {version ?? '—'}</p>
        <h2>Deterministic rule engine</h2>
        <p>Rules decide which transactions become cases. Weight changes score and ranking; enabled state and thresholds can change which transactions are flagged.</p></div>
      <button className="secondary-button" type="button" onClick={() => refresh().catch(() => {})}>↻ Refresh rules</button>
    </div>
    {error && <div className="inline-alert" role="alert">{error} <button type="button" onClick={() => refresh().catch(() => {})}>Retry</button></div>}
    <div className="rules-section-heading"><div><p className="eyebrow">Active configuration</p><h2>Rule settings</h2></div>
      <span>{loading ? 'Refreshing…' : `${rules.filter(rule => rule.enabled).length} of ${rules.length} enabled`}</span></div>
    <div className="rules-grid">
      {rules.map(rule => <RuleCard key={rule.rule_name} rule={rule} stats={statsByName[rule.rule_name]}
        analyst={analyst} onRefresh={refresh} />)}
      {!rules.length && <div className="rules-empty">{loading ? 'Loading current configuration…' : 'No rules available.'}</div>}
    </div>
    <section className="history-card">
      <div className="section-title-row"><div><p className="eyebrow">Audit trail</p><h2>Recent rule changes</h2></div><span className="section-kicker">Latest {history.length}</span></div>
      {history.length ? <div className="history-list">{history.map(entry => <div className="history-row" key={entry.id}>
        <span className="history-dot" aria-hidden="true" />
        <div><strong>{ruleLabel(entry.rule_name)}</strong><span>{historyChange(entry)}</span></div>
        <div className="history-meta"><strong>{entry.changed_by}</strong><span>{formatDate(entry.changed_at)}</span></div>
      </div>)}</div> : <p className="muted">No changes recorded.</p>}
    </section>
  </div>
}
