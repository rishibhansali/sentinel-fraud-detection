import { formatDate, formatScore, ruleLabel, statusLabel } from '../format.js'

const FILTERS = [
  ['pending', 'Pending'], ['open', 'Open'], ['in_review', 'In review'],
  ['decided', 'Decided'], ['all', 'All'],
]

export function CaseQueue({
  cases, totalMatches, selectedId, onSelect, filter, onFilter,
  query, onQuery, counts, onShowMore, syncing, error, onRefresh,
}) {
  return <section className="queue-panel" aria-label="Case queue">
    <div className="panel-heading">
      <div>
        <p className="eyebrow">Investigation queue</p>
        <h2>Cases <span className="heading-count">{counts.total}</span></h2>
      </div>
      <button className="icon-button" type="button" onClick={onRefresh} aria-label="Refresh cases" title="Refresh cases">
        <span aria-hidden="true">↻</span>
      </button>
    </div>
    <div className="queue-tools">
      <label className="search-field">
        <span aria-hidden="true">⌕</span>
        <input value={query} onChange={event => onQuery(event.target.value)}
          placeholder="Search transaction or user" aria-label="Search cases" />
      </label>
      <div className="filter-scroll" role="group" aria-label="Case status filter">
        {FILTERS.map(([value, label]) => <button key={value} type="button"
          className={`filter-tab ${filter === value ? 'active' : ''}`}
          aria-pressed={filter === value} onClick={() => onFilter(value)}>{label}</button>)}
      </div>
    </div>
    {error && <div className="inline-alert" role="alert">{error} <button type="button" onClick={onRefresh}>Retry</button></div>}
    <div className="queue-column-labels"><span>Case / signals</span><span>Priority</span></div>
    <div className="case-list">
      {cases.map(item => <button className={`case-row ${selectedId === item.id ? 'selected' : ''}`}
        type="button" key={item.id} onClick={() => onSelect(item.id)}
        aria-label={`Open case ${item.id}, ${statusLabel(item.status)}, priority ${formatScore(item.priority_score)}`}>
        <span className="case-row-main">
          <span className="case-row-top"><strong>TXN-{item.transaction_id}</strong><span className={`status-pill ${item.status}`}>{statusLabel(item.status)}</span></span>
          <span className="case-row-rule">{item.fired_rules?.length ? item.fired_rules.map(ruleLabel).join(' · ') : 'Rule evidence available in detail'}</span>
          <span className="case-row-meta">Case #{item.id} <span aria-hidden="true">·</span> {formatDate(item.flagged_at)}</span>
        </span>
        <span className="priority-cell"><strong>{formatScore(item.priority_score)}</strong><small>score</small></span>
      </button>)}
      {!cases.length && <div className="empty-list">
        <span className="empty-mark" aria-hidden="true">◌</span>
        <strong>{syncing ? 'Loading cases…' : 'No matching cases'}</strong>
        <p>{syncing ? 'Reconciling the live queue.' : 'Try another status or search term.'}</p>
      </div>}
    </div>
    {cases.length < totalMatches && <button className="show-more" type="button" onClick={onShowMore}>
      Show more cases <span aria-hidden="true">↓</span>
    </button>}
    <div className="queue-footer">{syncing ? 'Refreshing from the API…' : `${totalMatches} matching cases`}</div>
  </section>
}
