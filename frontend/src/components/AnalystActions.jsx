import { useState } from 'react'

export function AnalystActions({ detail, analyst, busy, error, onAction }) {
  const [note, setNote] = useState('')
  const [decision, setDecision] = useState('confirmed_fraud')
  const named = Boolean(analyst.trim())
  const isOwner = detail.claimed_by === analyst.trim()
  const canDecide = detail.status === 'in_review' ? isOwner :
    detail.status === 'confirmed_fraud' || detail.status === 'false_positive'

  return <section className="action-card" aria-label="Case actions">
    <div className="section-title-row"><h3>Review actions</h3><span className="section-kicker">Analyst workflow</span></div>
    {!named && <p className="action-guidance">Enter your analyst name in the header before taking an action.</p>}
    {error && <p className="inline-alert" role="alert">{error}</p>}
    {detail.status === 'open' && <div className="action-line">
      <p>Claim this case to record a decision.</p>
      <button className="primary-button" type="button" disabled={!named || busy}
        onClick={() => onAction('claim')}>{busy ? 'Working…' : 'Claim case'}</button>
    </div>}
    {detail.status === 'in_review' && !isOwner && <p className="action-guidance">
      This case is claimed by <strong>{detail.claimed_by || 'another analyst'}</strong>. Only the claimant can release or decide it.
    </p>}
    {detail.status === 'in_review' && isOwner && <div className="claim-owner">
      <span>Claimed by you</span>
      <button className="text-button" type="button" disabled={busy} onClick={() => onAction('release')}>Release claim</button>
    </div>}
    {canDecide && <form className="decision-form" onSubmit={event => {
      event.preventDefault()
      onAction('feedback', { decision, note })
    }}>
      {detail.status !== 'in_review' && <p className="action-guidance">A new decision will supersede the current recorded decision.</p>}
      <label>Decision
        <select value={decision} onChange={event => setDecision(event.target.value)}>
          <option value="confirmed_fraud">Confirm fraud</option>
          <option value="false_positive">Mark false positive</option>
        </select>
      </label>
      <label>Review note <span className="optional">optional</span>
        <textarea value={note} onChange={event => setNote(event.target.value)} rows="3"
          placeholder="Add context for the audit trail" />
      </label>
      <button className="primary-button" disabled={!named || busy} type="submit">
        {busy ? 'Saving…' : detail.status === 'in_review' ? 'Record decision' : 'Record correction'}
      </button>
    </form>}
  </section>
}
