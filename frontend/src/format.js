export function formatScore(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(3) : '—'
}

export function formatDate(value) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat(undefined, {
    month: 'short', day: 'numeric', year: 'numeric',
    hour: 'numeric', minute: '2-digit',
  }).format(date)
}

export function ruleLabel(value) {
  return String(value || '').replaceAll('_', ' ').replace(/\b\w/g, char => char.toUpperCase())
}

export function statusLabel(value) {
  return ({
    open: 'Open', in_review: 'In review',
    confirmed_fraud: 'Confirmed fraud', false_positive: 'False positive',
  })[value] || ruleLabel(value)
}
