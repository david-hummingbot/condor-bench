import { useState, useEffect, useCallback } from 'react'
import { getMatrix } from '../api.js'
import {
  fmtCost, fmtPct, fmtTokens, fmtSize, heatColor, inverseHeatColor,
  isToolDomain, orderedModels, stripToolPrefix,
} from '../utils.js'
import PageHeader from './PageHeader.jsx'

const COLOR_BY = [
  { id: 'pass_rate', label: 'Pass rate', fmt: fmtPct, higherIsBetter: true },
  { id: 'avg_composite', label: 'Composite', fmt: v => (v == null ? '—' : Number(v).toFixed(2)), higherIsBetter: true },
  { id: 'avg_total_tokens', label: 'Avg tokens', fmt: fmtTokens, higherIsBetter: false },
  { id: 'avg_cost_usd', label: 'Avg cost', fmt: fmtCost, higherIsBetter: false },
]

const AXIS_OPTS = [
  { id: 'domains', label: 'Routing domains' },
  { id: 'tools', label: 'Per tool' },
]

export default function Matrix() {
  const [matrix, setMatrix] = useState(null)
  const [axis, setAxis] = useState('domains')
  const [colorBy, setColorBy] = useState('pass_rate')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [hover, setHover] = useState(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setMatrix(await getMatrix())
    } catch (e) {
      setError(e.message)
      setMatrix(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const metric = COLOR_BY.find(c => c.id === colorBy) || COLOR_BY[0]
  const models = matrix ? orderedModels(matrix) : []
  const rowsSource = matrix?.[axis] || {}
  // Layer 2 buckets live on the domains axis but aren't routing targets, so they
  // are shown separately rather than mixed in with domains that get a config key.
  const rowKeys = Object.keys(rowsSource).sort()
  const routingRows = axis === 'domains' ? rowKeys.filter(k => !isToolDomain(k)) : rowKeys
  const toolBucketRows = axis === 'domains' ? rowKeys.filter(isToolDomain) : []

  return (
    <div>
      <PageHeader
        title="Matrix"
        description="Where each model is actually strong: one cell per model × routing domain, from that model’s most recent run. This is the evidence the Router turns into a config."
        meta={`${models.length} model${models.length !== 1 ? 's' : ''}${
          matrix?.generated_at ? ` · built ${new Date(matrix.generated_at).toLocaleString()}` : ''
        }`}
      />

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="matrix-controls">
          <Control label="Rows" opts={AXIS_OPTS} value={axis} onChange={setAxis} />
          <Control label="Colour by" opts={COLOR_BY} value={colorBy} onChange={setColorBy} />
          <button className="btn sm" onClick={load} disabled={loading} style={{ marginLeft: 'auto' }}>
            {loading ? '…' : '↻ Rebuild'}
          </button>
        </div>
        <div className="matrix-note">
          Cells show <strong>{metric.label.toLowerCase()}</strong> from each model's most recent run.
          The small number is how many cases were scored — a cell resting on two cases is
          weaker evidence than one resting on eight. Infra failures and harness artifacts are
          excluded, not scored zero.
          {axis === 'tools' && (
            <>
              {' '}Per-tool rows are judged by the Router on a <strong>lower bar than
              domains</strong> (see Router → Per-tool minimum): driving a tool is a coarser
              question than owning a domain, so the percentages here are not comparable to
              the domain axis.
            </>
          )}
        </div>
      </div>

      {error && (
        <div className="card"><div className="empty">{error}</div></div>
      )}

      {!error && matrix && (
        <>
          <MatrixTable
            title={axis === 'tools' ? 'Model × tool' : 'Model × routing domain'}
            rows={routingRows}
            cells={rowsSource}
            models={models}
            modelInfo={matrix.models}
            metric={metric}
            onHover={setHover}
            labelFor={stripToolPrefix}
          />
          {toolBucketRows.length > 0 && (
            <MatrixTable
              title="Capability buckets (not routing targets)"
              subtitle="Layer 2 groups. There is no Condor config key for “market data”, so the router skips these — per-tool verdicts live on the Per tool axis."
              rows={toolBucketRows}
              cells={rowsSource}
              models={models}
              modelInfo={matrix.models}
              metric={metric}
              onHover={setHover}
              labelFor={stripToolPrefix}
            />
          )}
          {hover && <CellDetail hover={hover} />}
        </>
      )}

      {!error && !matrix && !loading && (
        <div className="card"><div className="empty">No runs yet. Run a benchmark first.</div></div>
      )}
    </div>
  )
}

function Control({ label, opts, value, onChange }) {
  return (
    <div className="field" style={{ minWidth: 0 }}>
      <label>{label}</label>
      <div className="radio-group">
        {opts.map(o => (
          <button
            key={o.id}
            className={`radio-btn ${value === o.id ? 'active' : ''}`}
            onClick={() => onChange(o.id)}
          >
            {o.label}
          </button>
        ))}
      </div>
    </div>
  )
}

function MatrixTable({ title, subtitle, rows, cells, models, modelInfo, metric, onHover, labelFor }) {
  if (!rows.length) return null
  return (
    <div className="card">
      <div className="card-title">{title}</div>
      {subtitle && <div className="matrix-note" style={{ marginTop: -8, marginBottom: 14 }}>{subtitle}</div>}
      <div className="matrix-scroll">
        <table className="matrix-table">
          <thead>
            <tr>
              <th className="matrix-corner" />
              {models.map(m => {
                const info = modelInfo?.[m] || {}
                return (
                  <th key={m} title={m}>
                    <div className="matrix-model">{m.split(':').slice(1).join(':') || m}</div>
                    <div className="matrix-model-size">
                      {fmtSize(info.params_b)}
                      {info.in_registry === false && <span title="not in datasets/models.json — cannot be ranked by size, so it is excluded from routing"> ⚠</span>}
                    </div>
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody>
            {rows.map(row => {
              const rowCells = models.map(m => cells[row]?.[m])
              const values = rowCells.map(c => (c ? c[metric.id] : null))
              return (
                <tr key={row}>
                  <th className="matrix-row-label">{labelFor ? labelFor(row) : row}</th>
                  {models.map((m, i) => {
                    const cell = rowCells[i]
                    const value = values[i]
                    const bg = metric.higherIsBetter
                      ? heatColor(value)
                      : inverseHeatColor(value, values)
                    const blocked = cell?.destructive_failures?.length > 0
                    return (
                      <td
                        key={m}
                        className="matrix-cell"
                        style={{ background: bg }}
                        onMouseEnter={() => cell && onHover({ row: labelFor ? labelFor(row) : row, model: m, cell })}
                        onMouseLeave={() => onHover(null)}
                      >
                        {cell && cell.scored > 0 ? (
                          <>
                            <span className="matrix-value">{metric.fmt(value)}</span>
                            <span className="matrix-n">{cell.scored}</span>
                            {blocked && <span className="matrix-flag" title="a destructive case scored below the floor — blocks a routing recommendation">!</span>}
                          </>
                        ) : cell?.excluded ? (
                          <span className="matrix-excluded" title={(cell.excluded_reasons || []).join('\n')}>
                            {cell.excluded} excl
                          </span>
                        ) : (
                          <span className="matrix-empty">—</span>
                        )}
                      </td>
                    )
                  })}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function CellDetail({ hover }) {
  const { row, model, cell } = hover
  return (
    <div className="card">
      <div className="card-title">{row} × {model}</div>
      <div className="score-chips">
        <Chip label="Pass rate" value={fmtPct(cell.pass_rate)} />
        <Chip label="Composite" value={cell.avg_composite == null ? '—' : cell.avg_composite.toFixed(2)} />
        <Chip label="Scored" value={`${cell.scored}/${cell.cases}`} />
        <Chip label="Excluded" value={String(cell.excluded || 0)} />
        <Chip label="Avg tokens" value={fmtTokens(cell.avg_total_tokens)} />
        <Chip label="p95 tokens" value={fmtTokens(cell.p95_total_tokens)} />
        <Chip label="Avg cost" value={fmtCost(cell.avg_cost_usd)} />
        <Chip label="Avg latency" value={cell.avg_latency_s == null ? '—' : cell.avg_latency_s + 's'} />
      </div>
      {cell.destructive_failures?.length > 0 && (
        <div className="error-text" style={{ marginTop: 12 }}>
          Destructive case(s) below the floor: {cell.destructive_failures.join(', ')} — this
          blocks a routing recommendation regardless of the average.
        </div>
      )}
      {cell.excluded_reasons?.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="case-detail-label">Excluded from this cell</div>
          {cell.excluded_reasons.map((r, i) => (
            <div key={i} className="case-detail-text" style={{ color: 'var(--yellow)' }}>{r}</div>
          ))}
        </div>
      )}
      {cell.run_dir && (
        <div className="run-meta" style={{ marginTop: 12 }}>from run {cell.run_dir}</div>
      )}
    </div>
  )
}

function Chip({ label, value }) {
  return (
    <span className="score-chip">
      <span className="chip-label">{label}</span>
      <span className="chip-val">{value}</span>
    </span>
  )
}
