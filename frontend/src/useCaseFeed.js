import { useCallback, useEffect, useRef, useState } from 'react'
import { getCasePage } from './api.js'
import { applyCaseEvent, scanCases } from './caseFeed.js'

const WS_RETRY_MS = 2000
const REPAIR_MS = 60000

export function useCaseFeed() {
  const [cases, setCases] = useState([])
  const [connection, setConnection] = useState('connecting')
  const [syncing, setSyncing] = useState(true)
  const [error, setError] = useState(null)
  const [lastSynced, setLastSynced] = useState(null)
  const refreshRef = useRef(() => {})

  useEffect(() => {
    let disposed = false
    let socket = null
    let retryTimer = null
    let repairTimer = null
    let scanController = null
    let reconciling = false
    let rerun = false
    let pendingEvents = null
    let current = new Map()

    async function reconcile() {
      if (disposed) return
      if (reconciling) { rerun = true; return }
      reconciling = true
      pendingEvents = []
      scanController = new AbortController()
      setSyncing(true)
      try {
        const next = await scanCases(
          sinceId => getCasePage(sinceId, scanController.signal), pendingEvents,
        )
        if (disposed) return
        current = next
        setCases([...current.values()])
        setLastSynced(new Date())
        setError(null)
      } catch (cause) {
        if (disposed || cause?.name === 'AbortError') return
        for (const event of pendingEvents) applyCaseEvent(current, event)
        setCases([...current.values()])
        setError(cause.message || 'Could not refresh cases')
      } finally {
        pendingEvents = null
        reconciling = false
        scanController = null
        if (!disposed) {
          setSyncing(false)
          if (rerun) { rerun = false; queueMicrotask(reconcile) }
        }
      }
    }

    refreshRef.current = reconcile

    function connect() {
      if (disposed) return
      const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      let ws
      try {
        ws = new WebSocket(`${scheme}//${window.location.host}/ws/cases`)
      } catch {
        setConnection('reconnecting')
        reconcile()
        retryTimer = window.setTimeout(connect, WS_RETRY_MS)
        return
      }
      socket = ws
      let sawHello = false
      ws.onopen = () => { if (!disposed) setConnection('live') }
      ws.onmessage = message => {
        if (disposed) return
        let event
        try { event = JSON.parse(message.data) } catch { return }
        if (event.type === 'hello') {
          sawHello = true
          reconcile()
        } else if (event.type === 'resync') {
          reconcile()
        } else if (event.type === 'case.created' || event.type === 'case.updated') {
          if (pendingEvents) pendingEvents.push(event)
          else {
            current = applyCaseEvent(new Map(current), event)
            setCases([...current.values()])
          }
        }
      }
      ws.onclose = () => {
        if (disposed || socket !== ws) return
        setConnection('reconnecting')
        if (!sawHello) reconcile()
        retryTimer = window.setTimeout(connect, WS_RETRY_MS)
      }
      ws.onerror = () => { if (!disposed) setConnection('reconnecting') }
    }

    connect() // Open the WebSocket before the first REST reconciliation.
    repairTimer = window.setInterval(reconcile, REPAIR_MS)
    return () => {
      disposed = true
      refreshRef.current = () => {}
      window.clearTimeout(retryTimer)
      window.clearInterval(repairTimer)
      scanController?.abort()
      socket?.close()
    }
  }, [])

  const refresh = useCallback(() => refreshRef.current(), [])
  return { cases, connection, syncing, error, lastSynced, refresh }
}
