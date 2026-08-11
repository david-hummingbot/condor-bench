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

export function fmtPct(v) {
  if (v == null || isNaN(v)) return '—'
  return Math.round(Number(v) * 100) + '%'
}

export function fmtTokens(v) {
  if (v == null || isNaN(v)) return '—'
  const n = Number(v)
  if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k'
  return String(Math.round(n))
}

export function fmtCost(v) {
  if (v == null || isNaN(v)) return '—'
  return '$' + Number(v).toFixed(Number(v) < 0.01 ? 4 : 3)
}

export function fmtSize(paramsB) {
  return paramsB == null ? 'cloud' : `${paramsB}B`
}

/** Background for a heatmap cell. null (no data) is grey, never green. */
export function heatColor(v) {
  if (v == null || isNaN(v)) return 'var(--panel-2)'
  const n = Math.max(0, Math.min(1, Number(v)))
  // Red → orange → green ramp, alpha scaled so a low value reads as "bad" rather
  // than just "faint".
  if (n >= 0.8) return `rgba(39,195,110,${0.18 + n * 0.42})`
  if (n >= 0.5) return `rgba(230,126,34,${0.18 + n * 0.4})`
  return `rgba(232,64,64,${0.22 + (1 - n) * 0.34})`
}

/**
 * Colour scale for a "lower is better" metric (tokens, cost, latency).
 * Ranked within the row rather than against an absolute scale — token counts vary
 * by orders of magnitude between models, so a fixed ramp would show one colour.
 */
export function inverseHeatColor(v, values) {
  if (v == null || isNaN(v)) return 'var(--panel-2)'
  const nums = (values || []).filter(x => x != null && !isNaN(x)).map(Number)
  if (nums.length < 2) return 'rgba(79,140,255,0.18)'
  const min = Math.min(...nums)
  const max = Math.max(...nums)
  if (max === min) return 'rgba(79,140,255,0.18)'
  const t = (Number(v) - min) / (max - min)  // 0 = cheapest
  return heatColor(1 - t)
}

/** Models in registry order (smallest first), unranked last. */
export function orderedModels(matrix) {
  const models = matrix?.models || {}
  return Object.keys(models).sort((a, b) => {
    const ma = models[a] || {}
    const mb = models[b] || {}
    const ra = ma.in_registry ? 0 : 1
    const rb = mb.in_registry ? 0 : 1
    if (ra !== rb) return ra - rb
    const pa = ma.params_b == null ? Infinity : ma.params_b
    const pb = mb.params_b == null ? Infinity : mb.params_b
    if (pa !== pb) return pa - pb
    return a.localeCompare(b)
  })
}

/**
 * "0.45 quality · 0.20 tools · …" from the weights the backend actually scores with
 * (/api/config → scoring.weights). Read rather than hardcoded: a weights change in
 * config.py has to move the label, or the breakdown stops describing the composite
 * it sits next to. Empty string when config hasn't loaded yet.
 */
const WEIGHT_LABELS = {
  answer_quality: 'quality',
  tool_accuracy: 'tools',
  tool_params: 'params',
  live_validity: 'live validity',
  latency_score: 'latency',
}

export function weightSummary(weights) {
  return Object.entries(weights || {})
    .filter(([, w]) => w > 0)
    .map(([k, w]) => `${w} ${WEIGHT_LABELS[k] || k}`)
    .join(' · ')
}

export const isToolDomain = (d) => typeof d === 'string' && d.startsWith('tool:')
export const stripToolPrefix = (d) => (isToolDomain(d) ? d.slice(5) : d)
