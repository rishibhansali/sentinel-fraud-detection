import { AnalystActions } from './AnalystActions.jsx'
import { formatDate, formatScore, ruleLabel, statusLabel } from '../format.js'

function DetailEntries({ details }) {
  const entries = Object.entries(details || {})
  if (!entries.length) return <span className="muted">No additional measurements</span>
  return <dl className="detail-entry-grid">{entries.map(([key, value]) => <div key={key}>
    <dt>{ruleLabel(key)}</dt>
    <dd>{typeof value === 'object' && value !== null ? JSON.stringify(value) : String(value)}</dd>
  </div>)}</dl>
}

export function CaseDetail({ detail, loading, error, analyst, busy, actionError, onAction, onRetry }) {
  if (!detail) return <section className="detail-panel detail-empty" aria-label="Case detail">
    <div className="empty-mark large" aria-hidden="true">◎</div>
    <h2>{loading ? 'Loading case…' : error ? 'Case unavailable' : 'Select a case'}</h2>
    <p>{error || 'Choose a case from the queue to review its evidence and history.'}</p>
    {error && <button type="button" className="secondary-button" onClick={onRetry}>Retry</button>}
  </section>

  const rules = Array.isArray(detail.rule_results) ? detail.rule_results : []
  const fired = rules.filter(rule => rule.fired)
  const feedback = Array.isArray(detail.feedback) ? detail.feedback : []
  const adjustment = detail.priority_adjustment

  return <section className="detail-panel" aria-label={`Case ${detail.id} detail`}>
    <div className="detail-scroll">
      <div className="detail-heading">
        <div>
          <p className="eyebrow">Case #{detail.id} <span className="eyebrow-separator">/</span> TXN-{detail.transaction_id}</p>
          <h2>Transaction review</h2>
          <p className="detail-subtitle">Flagged {formatDate(detail.flagged_at)} · User {detail.user_id}</p>
        </div>
        <span className={`status-pill prominent ${detail.status}`}>{statusLabel(detail.status)}</span>
      </div>
      {error && <div className="inline-alert" role="alert">{error} <button type="button" onClick={onRetry}>Retry</button></div>}
      <div className="score-strip">
        <div><span>Priority score</span><strong>{formatScore(detail.priority_score)}</strong><small>Queue ranking</small></div>
        <div><span>Rule score</span><strong>{formatScore(detail.total_score)}</strong><small>Deterministic rules</small></div>
        <div><span>Rules fired</span><strong>{fired.length}</strong><small>of {rules.length} evaluated</small></div>
      </div>
      {adjustment && <div className="context-note">
        <strong>Priority adjusted by prior feedback</strong>
        <p>{adjustment.veto ? 'A prior confirmed fraud case prevented demotion.' :
          'Prior false-positive decisions lowered the queue priority. They did not change the flag or rule score.'}</p>
        <div className="adjustment-list">{(adjustment.per_rule || []).map(item => <span key={item.rule_name}>
          {ruleLabel(item.rule_name)} · {item.prior_false_positive_count} prior false positives · ×{item.factor}
        </span>)}</div>
      </div>}
      <div className="detail-section">
        <div className="section-title-row"><h3>Rule evidence</h3><span className="section-kicker">Why this case was flagged</span></div>
        <div className="rule-evidence-list">{fired.map(rule => <article className="evidence-card" key={rule.rule_name}>
          <div className="evidence-card-head"><span className="rule-dot" aria-hidden="true" /><strong>{ruleLabel(rule.rule_name)}</strong><span>+{formatScore(rule.sub_score)}</span></div>
          <DetailEntries details={rule.details} />
          {rule.rule_name === 'geo_impossibility' && <p className="synthetic-note">Location data in this dataset is synthetic.</p>}
        </article>)}</div>
      </div>
      <div className="annotations-grid">
        <section className="annotation-card">
          <p className="eyebrow">Optional model annotation</p>
          <h3>Anomaly score</h3>
          <strong>{formatScore(detail.ml_anomaly_score)}</strong>
          <p>{detail.ml_anomaly_score == null ? 'No model annotation for this case.' : 'Higher values are more anomalous. This is not a fraud probability.'}</p>
        </section>
        <section className="annotation-card ai-card">
          <p className="eyebrow">AI-generated context</p>
          <h3>Case explanation</h3>
          <p className="summary-text">{detail.ai_summary || 'No explanation generated for this case.'}</p>
          {detail.ai_summary && <small>{detail.ai_summary_model || 'Model not recorded'} · Generated {formatDate(detail.ai_summary_generated_at)}</small>}
          <p className="annotation-caveat">For review context only; it is not a fraud verdict.</p>
        </section>
      </div>
      <div className="detail-section feedback-section">
        <div className="section-title-row"><h3>Decision history</h3><span className="section-kicker">{feedback.length} entries</span></div>
        {feedback.length ? <ol className="feedback-list">{feedback.map(entry => <li key={entry.id}>
          <span className="feedback-marker" aria-hidden="true" />
          <div><strong>{statusLabel(entry.decision)}</strong> <span>by {entry.analyst}</span>
            <small>{formatDate(entry.decided_at)}</small>{entry.note && <p>{entry.note}</p>}</div>
        </li>)}</ol> : <p className="muted">No decision has been recorded.</p>}
      </div>
      <AnalystActions key={detail.id} detail={detail} analyst={analyst} busy={busy}
        error={actionError} onAction={onAction} />
    </div>
  </section>
}
