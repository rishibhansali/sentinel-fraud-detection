import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ApiError, claimCase, decideCase, getCaseDetail, releaseCase } from './api.js'
import { caseCounts, visibleCases } from './caseFeed.js'
import { CaseQueue } from './components/CaseQueue.jsx'
import { CaseDetail } from './components/CaseDetail.jsx'
import { RulesView } from './components/RulesView.jsx'
import { formatDate } from './format.js'
import { useCaseFeed } from './useCaseFeed.js'
import './styles.css'

export default function App() {
  const [view, setView] = useState('queue')
  const [analyst, setAnalyst] = useState(() => localStorage.getItem('sentinel-analyst') || '')
  const [filter, setFilter] = useState('pending')
  const [query, setQuery] = useState('')
  const [shown, setShown] = useState(50)
  const [selectedId, setSelectedId] = useState(null)
  const [detail, setDetail] = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [actionBusy, setActionBusy] = useState(false)
  const selectedIdRef = useRef(selectedId)
  selectedIdRef.current = selectedId

  const { cases, connection, syncing, error, lastSynced, refresh } = useCaseFeed()
  const counts = useMemo(() => caseCounts(cases), [cases])
  const matching = useMemo(() => visibleCases(cases, filter, query, Number.MAX_SAFE_INTEGER), [cases, filter, query])
  const visible = matching.slice(0, shown)
  const selectedSummary = cases.find(item => item.id === selectedId)

  useEffect(() => {
    setShown(50)
  }, [filter, query])

  useEffect(() => {
    if (!matching.length) { setSelectedId(null); return }
    if (!matching.some(item => item.id === selectedId)) setSelectedId(matching[0].id)
  }, [matching, selectedId])

  const loadDetail = useCallback(async (id, signal) => {
    if (id == null) { setDetail(null); return }
    setDetailLoading(true)
    try {
      const next = await getCaseDetail(id, signal)
      if (!signal?.aborted && selectedIdRef.current === id) {
        setDetail(next)
        setDetailError(null)
      }
    } catch (cause) {
      if (cause?.name !== 'AbortError' && selectedIdRef.current === id) {
        setDetailError(cause.message || 'Could not load case detail')
      }
    } finally {
      if (!signal?.aborted && selectedIdRef.current === id) setDetailLoading(false)
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    setActionError(null)
    if (selectedId == null) { setDetail(null); setDetailError(null); return () => controller.abort() }
    loadDetail(selectedId, controller.signal)
    return () => controller.abort()
  }, [selectedId, selectedSummary, loadDetail])

  async function handleAction(kind, payload = {}) {
    const name = analyst.trim()
    if (!name || selectedId == null) return
    setActionBusy(true)
    setActionError(null)
    try {
      if (kind === 'claim') await claimCase(selectedId, name)
      else if (kind === 'release') await releaseCase(selectedId, name)
      else if (kind === 'feedback') await decideCase(selectedId, name, payload.decision, payload.note)
      await Promise.all([refresh(), loadDetail(selectedId)])
    } catch (cause) {
      setActionError(cause.message || 'The action failed')
      if (cause instanceof ApiError && cause.status === 409) {
        await Promise.allSettled([refresh(), loadDetail(selectedId)])
      }
    } finally {
      setActionBusy(false)
    }
  }

  function updateAnalyst(value) {
    setAnalyst(value)
    localStorage.setItem('sentinel-analyst', value)
  }

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark" aria-hidden="true"><i /><i /><i /></span><span>sentinel<span className="brand-period">.</span></span></div>
      <div className="sidebar-label">WORKSPACE</div>
      <nav className="side-nav" aria-label="Primary navigation">
        <button type="button" className={view === 'queue' ? 'active' : ''}
          aria-current={view === 'queue' ? 'page' : undefined} onClick={() => setView('queue')}>
          <span className="nav-icon" aria-hidden="true">▤</span> Case queue <span className="nav-count">{counts.open + counts.in_review}</span>
        </button>
        <button type="button" className={view === 'rules' ? 'active' : ''}
          aria-current={view === 'rules' ? 'page' : undefined} onClick={() => setView('rules')}>
          <span className="nav-icon" aria-hidden="true">◈</span> Rule controls
        </button>
      </nav>
      <div className="sidebar-bottom">
        <span className="system-dot" /> Rule-based detection
        <small>Analyst names are not authenticated.</small>
      </div>
    </aside>

    <div className="main-area">
      <header className="topbar">
        <div className="breadcrumb">Operations <span>/</span> {view === 'queue' ? 'Case review' : 'Rule controls'}</div>
        <div className="topbar-right">
          <span className={`live-badge ${connection}`}><i />{connection === 'live' ? 'Live updates' : connection === 'connecting' ? 'Connecting' : 'Reconnecting'}</span>
          <label className="analyst-field"><span>Analyst</span><input value={analyst} onChange={event => updateAnalyst(event.target.value)}
            placeholder="Enter your name" autoComplete="name" aria-label="Analyst name" /></label>
        </div>
      </header>

      <main className="page-content">
        <div className="page-intro">
          <div><p className="eyebrow">Sentinel / Review workspace</p>
            <h1>{view === 'queue' ? 'Case review' : 'Rule controls'}</h1>
            <p>{view === 'queue' ? 'Investigate rule-flagged transactions and record your decisions.' :
              'Inspect signal quality and adjust deterministic rule settings.'}</p></div>
          <div className="sync-meta">{lastSynced ? `Last reconciled ${formatDate(lastSynced)}` : 'Waiting for first reconciliation'}</div>
        </div>

        {view === 'queue' ? <>
          <div className="metric-grid">
            <div className="metric-card"><span>Open cases</span><strong>{counts.open}</strong><small>Awaiting review</small><i className="metric-bar orange" /></div>
            <div className="metric-card"><span>In review</span><strong>{counts.in_review}</strong><small>Currently claimed</small><i className="metric-bar blue" /></div>
            <div className="metric-card"><span>Decided</span><strong>{counts.decided}</strong><small>Feedback recorded</small><i className="metric-bar teal" /></div>
          </div>
          <div className="review-grid">
            <CaseQueue cases={visible} totalMatches={matching.length} selectedId={selectedId}
              onSelect={setSelectedId} filter={filter} onFilter={setFilter}
              query={query} onQuery={setQuery} counts={counts}
              onShowMore={() => setShown(value => value + 50)}
              syncing={syncing} error={error} onRefresh={refresh} />
            <CaseDetail detail={detail?.id === selectedId ? detail : null} loading={detailLoading}
              error={detailError} analyst={analyst} busy={actionBusy} actionError={actionError}
              onAction={handleAction} onRetry={() => loadDetail(selectedId)} />
          </div>
        </> : <RulesView analyst={analyst.trim()} />}
      </main>
    </div>
  </div>
}
