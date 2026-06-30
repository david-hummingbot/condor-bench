export const PASS_THRESHOLD = 0.7

export function scoreColor(v) {
  if (v == null || isNaN(v)) return 'var(--muted)'
  if (v >= 0.8) return 'var(--green)'
  if (v >= 0.6) return 'var(--orange)'
  return 'var(--red)'
}

export function fmtScore(v) {
  if (v == null || isNaN(v)) return '—'
  return Number(v).toFixed(2)
}

export function fmtLatency(v) {
  if (v == null || isNaN(v)) return '—'
  return Number(v).toFixed(1) + 's'
}

export function fmtTime(ts) {
  if (!ts) return '—'
  const d = new Date(typeof ts === 'number' ? ts * 1000 : ts)
  return d.toLocaleString()
}

export function buildLeaderboard(runs) {
  const latest = {}
  for (const r of runs) {
    const key = r.model || r.run_dir
    if (!latest[key] || (r.timestamp || 0) > (latest[key].timestamp || 0)) {
      latest[key] = r
    }
  }
  return Object.values(latest).sort((a, b) => (b.composite_avg || 0) - (a.composite_avg || 0))
}

export function shortModel(modelKey) {
  if (!modelKey) return '?'
  const parts = modelKey.split(':')
  return parts.length > 1 ? parts.slice(1).join(':') : modelKey
}

export function providerOf(modelKey) {
  if (!modelKey) return ''
  return modelKey.split(':')[0]
}
